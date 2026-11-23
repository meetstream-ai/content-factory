"""HTTP API and review dashboard.

Run:  uvicorn contentfactory.api:app --reload      (then open http://127.0.0.1:8000)

Route groups (full reference with examples in docs/API.md):
  /api/v1/health                          mode, model, whether MeetStream/auth are configured
  /api/v1/settings                        OpenAI key + model and MeetStream key, set from the dashboard
  /api/v1/meetstream/recordings           MeetStream bots to pick from
  /api/v1/meetstream/resolve              check what a pasted link points at, before creating a job
  /api/v1/content-factory/jobs...         create from a link, poll, retry, transcript, recording,
                                          consent, export
  /api/v1/content-factory/uploads         create from a dropped-in recording file
  /api/v1/content-factory/assets/...      read, edit (PATCH), approve / reject / request-changes /
                                          reopen / regenerate, files
  /api/v1/content-factory/clips/{id}/preview   rendered clip for the player
  /                                       the review dashboard (static/index.html)

Long work never happens inside a request: job creation returns 202 with a job id and the
pipeline runs in the background. The exceptions are a clip edit (re-renders one short clip)
and "regenerate" (one model call), which are synchronous so the reviewer sees the result.

Error mapping: ReviewError / ExportError / retry-not-allowed -> 409, schema errors -> 422,
bad input -> 400, unsupported upload type -> 415, MeetStream unreachable -> 502.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from . import review
from .config import Settings, load_settings
from .db import Store, new_id
from .drafts import JobConfig
from .export import ExportError, build_package
from .meetstream import (
    MEDIA_EXTENSIONS, MeetStreamClient, MeetStreamError, RecordingLookupError, check_recording_url, classify_reference, resolve_recording,
    summarize_bot,
)
from .pipeline import REGENERABLE, STAGES, ContentFactory, PipelineError

STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- request bodies
# Defined at module level: with `from __future__ import annotations`, FastAPI can only resolve
# body models it can find in the module namespace.


class MeetStreamSource(BaseModel):
    """Which recording to process. Give either:

    * `recording_id` (or `bot_id`): the MeetStream bot id, or
    * `link`: a MeetStream URL containing the bot id, the meeting's own Zoom / Google Meet /
      Teams link (we find the bot that recorded it), a bare bot id, or a direct link to the
      recording file (e.g. MeetStream's S3 download link), which needs no MeetStream key.

    (To process a file from disk, POST it to /uploads instead.)
    """

    type: Literal["meetstream_recording"]
    bot_id: str | None = Field(default=None, alias="recording_id")
    link: str | None = None
    title: str | None = None
    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _one_reference(self):
        if not (self.bot_id or (self.link or "").strip()):
            raise ValueError("Give either recording_id (the MeetStream bot id) or link.")
        return self


class CreateJob(BaseModel):
    source: MeetStreamSource
    configuration: JobConfig = Field(default_factory=JobConfig)


class ReviewerNote(BaseModel):
    reviewer: str = "reviewer"
    note: str | None = None
    feedback: str | None = None


class AssetPatch(BaseModel):
    reviewer: str = "reviewer"
    title: str | None = None
    content: dict[str, Any] | None = None


class SettingsPatch(BaseModel):
    """Fields left out are unchanged; an empty string removes a saved value."""

    openai_api_key: str | None = None
    openai_model: str | None = None
    meetstream_api_key: str | None = None


# ---------------------------------------------------------------- key checks
# Run before a key is saved, so a typo is caught in the Settings panel rather than as a failed
# job. Module-level so tests can replace them.


def check_openai_key(key: str, model: str) -> None:
    """Raise HTTPException unless `key` works and can use `model` (one cheap models.retrieve call)."""
    import openai

    try:
        openai.OpenAI(api_key=key, max_retries=0, timeout=20).models.retrieve(model)
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        raise HTTPException(422, "OpenAI didn't accept that key. Copy it again from platform.openai.com/api-keys.")
    except openai.NotFoundError:
        raise HTTPException(422, f"That key works, but it can't use the model {model}. Try another model, e.g. gpt-5-mini.")
    except openai.APIConnectionError:
        raise HTTPException(502, "Couldn't reach OpenAI to check the key. Check the internet connection.")
    except openai.APIStatusError as e:
        raise HTTPException(502, f"OpenAI returned an error while checking the key (HTTP {e.status_code}).")


def check_meetstream_key(key: str, base_url: str) -> None:
    """Raise HTTPException unless MeetStream accepts `key` (one List Bots call)."""
    client = MeetStreamClient(key, base_url)
    try:
        client.list_bots()
    except MeetStreamError as e:
        if e.status in (401, 403):
            raise HTTPException(422, "MeetStream didn't accept that key. Copy it again from app.meetstream.ai/api-key.")
        raise HTTPException(502, f"Couldn't check the key with MeetStream: {e}")
    finally:
        client.close()


def _key_view(value: str | None, saved: bool) -> dict:
    """How a key is shown back: whether it's set, where from, and its last 4 characters. Never the key."""
    return {"set": bool(value), "source": ("saved" if saved else "environment") if value else None,
            "hint": f"…{value[-4:]}" if value and len(value) > 8 else ("set" if value else None)}


def create_app(settings: Settings | None = None, factory: ContentFactory | None = None) -> FastAPI:
    """Build the FastAPI app. Tests pass their own settings (temp data dir) and factory (mocks)."""
    settings = settings or load_settings()
    factory = factory or ContentFactory(settings, Store(settings.db_path))
    store = factory.store
    api_token = os.environ.get("CF_API_TOKEN")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        factory.shutdown()

    app = FastAPI(title="MeetStream Content Factory", version="0.1.0", lifespan=lifespan)
    app.state.factory = factory

    def auth(authorization: str | None = Header(default=None), access_token: str | None = None) -> None:
        # <video>/<audio>/download links can't send headers, so media URLs may carry ?access_token=.
        if not api_token:
            return
        supplied = authorization.removeprefix("Bearer ") if authorization else access_token
        if not (supplied and secrets.compare_digest(supplied, api_token)):
            raise HTTPException(401, "Missing or invalid API token")

    guarded = [Depends(auth)]
    P = "/api/v1/content-factory"

    def job_or_404(job_id: str) -> dict:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    def asset_or_404(asset_id: str) -> dict:
        asset = store.get_asset(asset_id)
        if asset is None:
            raise HTTPException(404, "Asset not found")
        return asset

    def job_view(job: dict) -> dict:
        """The job as the API returns it: status/progress plus webinar summary, asset counts, exports."""
        webinar = store.get_webinar(job["webinar_id"]) if job["webinar_id"] else None
        done = job["completed"]
        counts: dict[str, dict[str, int]] = {}
        for a in store.list_assets(job["id"]):
            counts.setdefault(a["asset_type"], {}).setdefault(a["status"], 0)
            counts[a["asset_type"]][a["status"]] += 1
        return {
            "job_id": job["id"], "status": job["status"], "progress": job["progress"],
            "current_stage": job["current_stage"], "completed": done,
            "pending": [s for s, _ in STAGES if s not in done], "error_message": job["error_message"],
            "warnings": job["warnings"], "generator": job["generator"], "configuration": job["configuration"],
            "source": job["source"], "consent_confirmed": bool(job["consent_confirmed"]),
            "consent_note": job["consent_note"], "created_at": job["created_at"], "completed_at": job["completed_at"],
            "webinar": webinar and {k: webinar[k] for k in ("id", "title", "source_type", "source_ref",
                                                             "duration_seconds", "has_video")},
            "asset_counts": counts, "exports": store.list_exports(job["id"]),
        }

    def asset_view(asset: dict, detail: bool = False) -> dict:
        """An asset plus what the UI needs: allowed decisions, file URLs, and (detail) versions + review log."""
        decisions = [d for d in review.allowed_decisions(asset["status"])
                     if d != "regenerate" or asset["asset_type"] in REGENERABLE]
        out = {**asset, "allowed_decisions": decisions,
               "file_urls": {k: f"{P}/assets/{asset['id']}/files/{k}" for k in asset["files"]}}
        if detail:
            out["versions"] = store.list_versions(asset["id"])
            out["reviews"] = store.list_reviews(asset["id"])
        return out

    # ------------------------------------------------------------ health

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"ok": True, "llm_mode": settings.llm_mode, "model": settings.model if settings.llm_mode == "openai" else None,
                "meetstream_configured": bool(settings.meetstream_api_key), "auth_required": bool(api_token),
                "local_transcription": settings.whisper_model}

    # ------------------------------------------------------------ settings

    def settings_view() -> dict:
        try:
            saved = json.loads(settings.secrets_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
        return {"openai_api_key": _key_view(settings.openai_api_key, "openai_api_key" in saved),
                "openai_model": settings.model,
                "meetstream_api_key": _key_view(settings.meetstream_api_key, "meetstream_api_key" in saved),
                "llm_mode": settings.llm_mode}

    @app.get("/api/v1/settings", dependencies=guarded)
    def get_settings() -> dict:
        return settings_view()

    @app.post("/api/v1/settings", dependencies=guarded)
    def update_settings(body: SettingsPatch) -> dict:
        """Check new keys with their service, then save them (to data/secrets.json) and use them for the next job."""
        changes = body.model_dump(exclude_none=True)
        model = (changes.get("openai_model") or "").strip() or settings.model
        new_openai = (changes.get("openai_api_key") or "").strip()
        if new_openai or ("openai_model" in changes and settings.openai_api_key):
            check_openai_key(new_openai or settings.openai_api_key, model)
        if (changes.get("meetstream_api_key") or "").strip():
            check_meetstream_key(changes["meetstream_api_key"].strip(), settings.meetstream_base_url)
        settings.save(**changes)
        factory.settings_changed()
        return settings_view()

    # ------------------------------------------------------------ MeetStream recordings

    @app.get("/api/v1/meetstream/recordings", dependencies=guarded)
    def meetstream_recordings(status: str | None = None, platform: str | None = None,
                              date_from: str | None = None, date_to: str | None = None) -> dict:
        try:
            client = factory.meetstream()
        except MeetStreamError as e:
            raise HTTPException(400, str(e))
        try:
            page = client.list_bots(status=status, platform=platform, date_from=date_from, date_to=date_to)
        except MeetStreamError as e:
            raise HTTPException(502, str(e))
        finally:
            client.close()
        return {"recordings": [summarize_bot(b) for b in page.get("bots", [])],
                "has_next_page": page.get("hasNextPage", False), "next_cursor": page.get("nextCursor")}

    # ------------------------------------------------------------ jobs

    def lookup(ref: str) -> dict:
        """What a pasted link or id points at, checked now; failures become HTTP errors with the reason.

        Recording links are checked by fetching their first byte and need no MeetStream key;
        everything else is resolved through MeetStream.
        """
        try:
            kind, value = classify_reference(ref or "")
        except RecordingLookupError as e:
            raise HTTPException(422, str(e))
        if kind == "recording_url":
            try:
                info = check_recording_url(value, factory.url_transport)
            except RecordingLookupError as e:
                raise HTTPException(422, str(e))
            uses_meetstream = bool(info["bot_id"] and settings.meetstream_api_key)
            return {"resolved_from": "recording_url", "url": value, "bot_id": info["bot_id"], "bot": None,
                    "other_matches": [], "expires_at": info["expires_at"], "size": info["size"],
                    "transcript": "meetstream" if uses_meetstream else "local"}
        if not settings.meetstream_api_key:
            raise HTTPException(422, "MeetStream isn't connected (MEETSTREAM_API_KEY isn't set on the server), so meeting "
                                     "links and bot ids can't be looked up. Paste the recording's download link, or drop the file in.")
        client = factory.meetstream()
        try:
            r = resolve_recording(client, ref)
        except RecordingLookupError as e:  # the link doesn't lead to a recording: the user's to fix
            raise HTTPException(422, str(e))
        except MeetStreamError as e:  # MeetStream refused or failed
            raise HTTPException(502, str(e))
        finally:
            client.close()
        return {"bot_id": r.bot_id, "resolved_from": r.resolved_from, "bot": r.bot, "other_matches": r.other_matches,
                "transcript": "meetstream"}

    @app.get("/api/v1/meetstream/resolve", dependencies=guarded)
    def resolve_link(link: str) -> dict:
        """Preview which recording a pasted link refers to, before starting a job."""
        return lookup(link)

    @app.post(f"{P}/jobs", status_code=202, dependencies=guarded)
    def create_job(body: CreateJob) -> dict:
        # Check now, so a reference that leads to no recording fails immediately rather than in a job.
        found = lookup(body.source.link or body.source.bot_id)
        if found["resolved_from"] == "recording_url":
            source = {"type": "recording_url", "url": found["url"], "bot_id": found["bot_id"], "title": body.source.title}
        else:
            source = {**body.source.model_dump(), "bot_id": found["bot_id"], "resolved_from": found["resolved_from"]}
        return job_view(factory.submit(source, body.configuration))

    @app.post(f"{P}/uploads", status_code=202, dependencies=guarded)
    def upload_job(recording: UploadFile = File(...), title: str = Form(default=""),
                   configuration: str = Form(default="{}")) -> dict:
        """Create a job from a dropped-in recording file. It is transcribed locally."""
        ext = Path(recording.filename or "").suffix.lower()
        if ext not in MEDIA_EXTENSIONS:
            raise HTTPException(415, f"{recording.filename}: not a recording file this app reads ({', '.join(MEDIA_EXTENSIONS)}).")
        try:
            config = JobConfig.model_validate(json.loads(configuration or "{}"))
        except (json.JSONDecodeError, ValidationError) as e:
            raise HTTPException(422, f"Invalid configuration: {e}")
        limit = settings.max_upload_mb * 1024 * 1024
        if recording.size is not None and recording.size > limit:
            raise HTTPException(413, f"{recording.filename}: larger than the {settings.max_upload_mb} MB upload limit (CF_MAX_UPLOAD_MB).")
        dest = settings.data_dir / "uploads" / new_id("up") / f"recording{ext}"
        dest.parent.mkdir(parents=True)
        written = 0
        with dest.open("wb") as out:
            while chunk := recording.file.read(1 << 20):
                written += len(chunk)
                if written > limit:
                    break
                out.write(chunk)
        if written > limit:
            shutil.rmtree(dest.parent, ignore_errors=True)
            raise HTTPException(413, f"{recording.filename}: larger than the {settings.max_upload_mb} MB upload limit (CF_MAX_UPLOAD_MB).")
        source = {"type": "upload", "upload_path": str(dest), "filename": recording.filename, "title": title.strip() or None}
        return job_view(factory.submit(source, config))

    @app.get(f"{P}/jobs", dependencies=guarded)
    def list_jobs() -> list[dict]:
        def summary(j: dict) -> dict:
            statuses = [a["status"] for a in store.list_assets(j["id"])]
            return {"job_id": j["id"], "status": j["status"], "progress": j["progress"], "title": j["webinar_title"]
                    or j["source"].get("title") or j["source"].get("bot_id"), "created_at": j["created_at"],
                    # Review progress, so a package list can show "5 of 29 approved" without loading assets.
                    "assets": len(statuses), "approved": statuses.count(review.APPROVED),
                    "to_review": statuses.count(review.PENDING_REVIEW)}
        return [summary(j) for j in store.list_jobs()]

    @app.get(f"{P}/jobs/{{job_id}}", dependencies=guarded)
    def get_job(job_id: str) -> dict:
        return job_view(job_or_404(job_id))

    @app.post(f"{P}/jobs/{{job_id}}/retry", status_code=202, dependencies=guarded)
    def retry_job(job_id: str) -> dict:
        job_or_404(job_id)
        try:
            return job_view(factory.retry(job_id))
        except PipelineError as e:
            raise HTTPException(409, str(e))

    @app.get(f"{P}/jobs/{{job_id}}/assets", dependencies=guarded)
    def list_assets(job_id: str, asset_type: str | None = None) -> list[dict]:
        job_or_404(job_id)
        return [asset_view(a) for a in store.list_assets(job_id, asset_type)]

    @app.get(f"{P}/jobs/{{job_id}}/transcript", dependencies=guarded)
    def get_transcript(job_id: str) -> dict:
        job = job_or_404(job_id)
        webinar = store.get_webinar(job["webinar_id"]) if job["webinar_id"] else None
        if not webinar or not webinar["transcript_path"]:
            raise HTTPException(404, "Transcript not ready")
        return json.loads(Path(webinar["transcript_path"]).read_text())

    @app.get(f"{P}/jobs/{{job_id}}/recording", dependencies=guarded)
    def get_recording(job_id: str) -> FileResponse:
        job = job_or_404(job_id)
        webinar = store.get_webinar(job["webinar_id"]) if job["webinar_id"] else None
        if not webinar or not webinar["recording_path"]:
            raise HTTPException(404, "No recording for this job")
        return FileResponse(webinar["recording_path"])

    @app.post(f"{P}/jobs/{{job_id}}/consent", dependencies=guarded)
    def confirm_consent(job_id: str, body: ReviewerNote) -> dict:
        job_or_404(job_id)
        return job_view(factory.confirm_consent(job_id, body.reviewer, body.note))

    @app.post(f"{P}/jobs/{{job_id}}/export", dependencies=guarded)
    def export_job(job_id: str) -> dict:
        job = job_or_404(job_id)
        webinar = store.get_webinar(job["webinar_id"]) if job["webinar_id"] else None
        approved = [a["id"] for a in store.list_assets(job_id) if a["status"] == review.APPROVED]
        try:
            path = build_package(store, job_id, settings.job_dir(job_id),
                                 Path(webinar["transcript_path"]) if webinar and webinar["transcript_path"] else None)
        except ExportError as e:
            raise HTTPException(409, str(e))
        except OSError as e:  # disk problems: record and let the user retry; nothing is regenerated
            store.add_export(job_id, "zip", approved, "failed", error=str(e))
            raise HTTPException(500, f"Export failed: {e}")
        exp = store.add_export(job_id, "zip", approved, "completed", path=str(path))
        return {**exp, "download_url": f"{P}/exports/{exp['id']}/download"}

    @app.get(f"{P}/exports/{{export_id}}/download", dependencies=guarded)
    def download_export(export_id: str) -> FileResponse:
        exp = store.get_export(export_id)
        if not exp or not exp["path"] or not Path(exp["path"]).exists():
            raise HTTPException(404, "Export not found")
        return FileResponse(exp["path"], filename=Path(exp["path"]).name, media_type="application/zip")

    # ------------------------------------------------------------ assets

    @app.get(f"{P}/assets/{{asset_id}}", dependencies=guarded)
    def get_asset(asset_id: str) -> dict:
        return asset_view(asset_or_404(asset_id), detail=True)

    @app.get(f"{P}/assets/{{asset_id}}/files/{{kind}}", dependencies=guarded)
    def get_asset_file(asset_id: str, kind: str) -> FileResponse:
        asset = asset_or_404(asset_id)
        rel = asset["files"].get(kind)
        if not rel:
            raise HTTPException(404, f"No {kind} file for this asset")
        # Paths come from our own DB, but check containment anyway so a bad row can't serve /etc/passwd.
        root = settings.job_dir(asset["job_id"]).resolve()
        path = (root / rel).resolve()
        if root not in path.parents or not path.exists():
            raise HTTPException(404, "File not found")
        media_type = {".vtt": "text/vtt", ".srt": "text/plain"}.get(path.suffix)
        return FileResponse(path, media_type=media_type)

    @app.get(f"{P}/clips/{{asset_id}}/preview", dependencies=guarded)
    def preview_clip(asset_id: str, format: str = "landscape") -> FileResponse:
        """The rendered clip in `format`; audio-only sources serve their audio excerpt instead."""
        asset = asset_or_404(asset_id)
        if asset["asset_type"] != "clip":
            raise HTTPException(400, "Not a clip")
        kind = format if format in asset["files"] else ("audio" if "audio" in asset["files"] else format)
        return get_asset_file(asset_id, kind)

    def run_review(fn, *args):
        """Call a review action and map its errors to HTTP status codes."""
        try:
            return asset_view(fn(*args), detail=True)
        except review.ReviewError as e:
            raise HTTPException(409, str(e))
        except ValidationError as e:
            raise HTTPException(422, json.loads(e.json()))
        except PipelineError as e:
            raise HTTPException(400, str(e))

    @app.patch(f"{P}/assets/{{asset_id}}", dependencies=guarded)
    def update_asset(asset_id: str, body: AssetPatch) -> dict:
        asset_or_404(asset_id)
        return run_review(factory.edit, asset_id, body.reviewer, body.title, body.content)

    # One POST route per decision; `_decision=decision` binds the loop variable at definition time.
    for route, decision in (("approve", "approve"), ("reject", "reject"),
                            ("request-changes", "request_changes"), ("reopen", "reopen")):
        def handler(asset_id: str, body: ReviewerNote, _decision=decision) -> dict:
            asset_or_404(asset_id)
            return run_review(factory.decide, asset_id, _decision, body.reviewer, body.feedback)

        app.post(f"{P}/assets/{{asset_id}}/{route}", dependencies=guarded, name=f"asset_{decision}")(handler)

    @app.post(f"{P}/assets/{{asset_id}}/regenerate", dependencies=guarded)
    def regenerate_asset(asset_id: str, body: ReviewerNote) -> dict:
        asset_or_404(asset_id)
        try:
            return run_review(factory.regenerate, asset_id, body.reviewer, body.feedback or "")
        except Exception as e:  # generation failures shouldn't look like server crashes
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(502, f"Regeneration failed: {e}")

    # ------------------------------------------------------------ UI

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    return app


# Module-level app for `uvicorn contentfactory.api:app`. CF_NO_APP=1 (set by the tests) skips it so
# importing this module doesn't create ./data.
app = create_app() if os.environ.get("CF_NO_APP") != "1" else None
