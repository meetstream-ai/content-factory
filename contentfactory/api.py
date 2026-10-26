"""HTTP API and review dashboard.

Run:  uvicorn contentfactory.api:app --reload      (then open http://127.0.0.1:8000)

Route groups (full reference with examples in docs/API.md):
  /api/v1/health                          mode, model, whether MeetStream/auth are configured
  /api/v1/meetstream/recordings           MeetStream bots to pick from
  /api/v1/content-factory/jobs...         create (MeetStream or upload), poll, retry, transcript,
                                          recording, consent, export
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
from pydantic import BaseModel, Field, ValidationError

from . import review
from .config import Settings, load_settings
from .db import Store, new_id
from .drafts import JobConfig
from .export import ExportError, build_package
from .meetstream import MeetStreamError, summarize_bot
from .pipeline import REGENERABLE, STAGES, ContentFactory, PipelineError

STATIC = Path(__file__).parent / "static"
# Upload allow-lists, by extension. FFmpeg would accept more, but these are what we test.
ALLOWED_MEDIA = {".mp4", ".mov", ".webm", ".mkv", ".mp3", ".wav", ".m4a"}
ALLOWED_TRANSCRIPTS = {".json", ".srt", ".vtt"}


# ---------------------------------------------------------------- request bodies
# Defined at module level: with `from __future__ import annotations`, FastAPI can only resolve
# body models it can find in the module namespace.


class MeetStreamSource(BaseModel):
    """`recording_id` (as in the product spec) and `bot_id` are accepted interchangeably."""

    type: Literal["meetstream_recording"]
    bot_id: str = Field(min_length=1, alias="recording_id")
    title: str | None = None
    model_config = {"populate_by_name": True}


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
        return {"ok": True, "llm_mode": settings.llm_mode, "model": settings.model if settings.llm_mode == "claude" else None,
                "meetstream_configured": bool(settings.meetstream_api_key), "auth_required": bool(api_token)}

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

    @app.post(f"{P}/jobs", status_code=202, dependencies=guarded)
    def create_job(body: CreateJob) -> dict:
        if not settings.meetstream_api_key:
            raise HTTPException(400, "MEETSTREAM_API_KEY is not configured on the server.")
        job = factory.submit(body.source.model_dump(), body.configuration)
        return job_view(job)

    @app.post(f"{P}/uploads", status_code=202, dependencies=guarded)
    def upload_job(recording: UploadFile | None = File(default=None), transcript: UploadFile | None = File(default=None),
                   title: str = Form(default="Untitled webinar"), configuration: str = Form(default="{}")) -> dict:
        if recording is None and transcript is None:
            raise HTTPException(400, "Upload a recording, a transcript, or both.")
        try:
            config = JobConfig.model_validate(json.loads(configuration or "{}"))
        except (json.JSONDecodeError, ValidationError) as e:
            raise HTTPException(422, f"Invalid configuration: {e}")
        updir = settings.data_dir / "uploads" / new_id("up")
        updir.mkdir(parents=True, exist_ok=True)
        source: dict[str, Any] = {"type": "upload", "title": title}
        for f, allowed, key in ((recording, ALLOWED_MEDIA, "recording_path"), (transcript, ALLOWED_TRANSCRIPTS, "transcript_path")):
            if f is None:
                continue
            ext = Path(f.filename or "").suffix.lower()
            if ext not in allowed:
                raise HTTPException(415, f"{f.filename}: unsupported type (allowed: {', '.join(sorted(allowed))})")
            dest = updir / f"{key.split('_')[0]}{ext}"
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            source[key] = str(dest)
            if key == "recording_path":
                source["filename"] = f.filename
        return job_view(factory.submit(source, config))

    @app.get(f"{P}/jobs", dependencies=guarded)
    def list_jobs() -> list[dict]:
        return [{"job_id": j["id"], "status": j["status"], "progress": j["progress"], "title": j["webinar_title"]
                 or j["source"].get("title") or j["source"].get("bot_id"), "created_at": j["created_at"]}
                for j in store.list_jobs()]

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
