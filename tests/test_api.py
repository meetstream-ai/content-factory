"""End to end through the HTTP API: ingest, generate (offline), render with FFmpeg, review, export."""

import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from contentfactory.api import create_app
from contentfactory.config import Settings
from contentfactory.db import Store
from contentfactory.ingest import parse_transcript
from contentfactory.meetstream import MeetStreamClient
from contentfactory.pipeline import ContentFactory

from .conftest import make_meetstream_transcript

P = "/api/v1/content-factory"
OTHER_BOT = "5b6c7d8e-1f2a-4b3c-9d4e-5f6a7b8c9d0e"
CONFIG = {"clip_count": 3, "min_clip_seconds": 5, "max_clip_seconds": 20, "clip_formats": ["landscape", "portrait"]}


def fake_meetstream(video_bytes: bytes, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if request.url.host.endswith(".s3.amazonaws.com"):
            assert "authorization" not in request.headers  # presigned URLs carry their own auth
            if request.headers.get("range") == "bytes=0-0":  # the up-front link check
                return httpx.Response(206, content=video_bytes[:1], headers={
                    "content-type": "video/mp4", "content-range": f"bytes 0-0/{len(video_bytes)}"})
            return httpx.Response(200, content=video_bytes)
        assert request.headers["authorization"] == "Token ms_key"
        if path in ("/api/v1/bots/bot_42/detail", f"/api/v1/bots/{OTHER_BOT}/detail"):
            bot_id = path.split("/")[-2]
            return httpx.Response(200, json={"bot_details": {"BotID": bot_id, "Platform": "Zoom", "CreatedAt": "2026-10-21T15:00:00Z",
                                                             "transcript_id": f"tr_{bot_id}"}})
        if path in ("/api/v1/transcript/tr_bot_42/get_transcript", f"/api/v1/transcript/tr_{OTHER_BOT}/get_transcript"):
            return httpx.Response(200, json=make_meetstream_transcript())
        if path == "/api/v1/bots/bot_42/get_video":
            return httpx.Response(200, json={"video_url": "https://recordings.s3.amazonaws.com/rec.mp4", "video_info": {}})
        if path == "/api/v1/bots":
            return httpx.Response(200, json={"bots": [
                {"bot_id": "bot_42", "status": "Stopped", "platform": "Zoom", "created_at": "2026-10-21T15:00:00Z",
                 "meeting_link": "https://us02web.zoom.us/j/8812345678?pwd=abc"},
                {"bot_id": "bot_7", "status": "Stopped", "platform": "Zoom", "created_at": "2026-09-01T15:00:00Z",
                 "meeting_link": "https://zoom.us/j/8812345678"},
            ], "hasNextPage": False})
        return httpx.Response(404, json={"message": "not found"})
    return httpx.MockTransport(handler)


def fake_transcriber(calls: list):
    """Stands in for local speech-to-text (faster-whisper) so tests don't download a model."""
    def transcribe(webinar_id, path, duration):
        calls.append(f"transcribe:{Path(path).name}")
        return parse_transcript(webinar_id, json.dumps(make_meetstream_transcript()), duration)
    return transcribe


@pytest.fixture
def env(tmp_path, tiny_video):
    calls: list[str] = []
    settings = Settings(data_dir=tmp_path, llm_mode="offline", meetstream_api_key="ms_key", job_workers=1)
    transport = fake_meetstream(tiny_video.read_bytes(), calls)
    factory = ContentFactory(settings, Store(settings.db_path),
                             meetstream_factory=lambda: MeetStreamClient("ms_key", transport=transport),
                             transcriber=fake_transcriber(calls), url_transport=transport)
    with TestClient(create_app(settings, factory)) as client:
        yield client, calls


def wait(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"{P}/jobs/{job_id}").json()
        if job["status"] in ("awaiting_review", "failed"):
            return job
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def by_type(assets, t):
    return [a for a in assets if a["asset_type"] == t]


def test_meetstream_recording_to_exported_package(env):
    client, calls = env
    assert client.get("/api/v1/meetstream/recordings").json()["recordings"][0]["bot_id"] == "bot_42"

    r = client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording", "recording_id": "bot_42"}, "configuration": CONFIG})
    assert r.status_code == 202
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "awaiting_review", job["error_message"]
    assert job["progress"] == 100 and job["pending"] == []
    assert job["webinar"]["title"] == "Zoom recording 2026-10-21" and job["webinar"]["has_video"]
    assert "/api/v1/transcript/tr_bot_42/get_transcript" in calls
    assert any(w["code"] == "sensitive_content" for w in job["warnings"])

    assets = client.get(f"{P}/jobs/{job['job_id']}/assets").json()
    assert {a["status"] for a in assets} == {"PENDING_REVIEW"}  # nothing skips review
    types = {a["asset_type"] for a in assets}
    assert {"chapters", "content_brief", "clip", "blog", "linkedin_post", "quote", "visual_brief"} <= types

    clip = by_type(assets, "clip")[0]
    assert "regenerate" not in by_type(assets, "quote")[0]["allowed_decisions"]
    assert "regenerate" in by_type(assets, "blog")[0]["allowed_decisions"]
    assert {"landscape", "portrait", "srt", "vtt"} <= set(clip["files"])
    assert not [n for n in clip["qa"] if n["level"] == "error"]
    video = client.get(f"{P}/clips/{clip['id']}/preview?format=portrait")
    assert video.status_code == 200 and video.content[4:8] == b"ftyp"
    vtt = client.get(clip["file_urls"]["vtt"]).text
    assert vtt.startswith("WEBVTT")

    for q in by_type(assets, "quote"):  # every quote is verbatim from the transcript
        transcript = client.get(f"{P}/jobs/{job['job_id']}/transcript").json()
        spoken = " ".join(s["text"] for s in transcript["segments"]).lower()
        assert q["content"]["quote"].lower().rstrip(".") in spoken

    # Export is blocked until consent is confirmed and something is approved.
    assert client.post(f"{P}/jobs/{job['job_id']}/export").status_code == 409
    client.post(f"{P}/jobs/{job['job_id']}/consent", json={"reviewer": "ana", "note": "speakers signed release"})
    assert "Nothing is approved" in client.post(f"{P}/jobs/{job['job_id']}/export").json()["detail"]

    blog = by_type(assets, "blog")[0]
    post = by_type(assets, "linkedin_post")[0]
    assert client.post(f"{P}/assets/{clip['id']}/approve", json={"reviewer": "ana"}).json()["status"] == "APPROVED"
    assert client.post(f"{P}/assets/{blog['id']}/approve", json={"reviewer": "ana"}).json()["status"] == "APPROVED"
    assert client.post(f"{P}/assets/{post['id']}/reject", json={"reviewer": "ana"}).json()["status"] == "REJECTED"

    exp = client.post(f"{P}/jobs/{job['job_id']}/export").json()
    z = zipfile.ZipFile(io.BytesIO(client.get(exp["download_url"]).content))
    names = set(z.namelist())
    assert {"content-package/manifest.json", "content-package/blog.md", "content-package/source/transcript.json",
            "content-package/clips/clip_01_landscape.mp4", "content-package/captions/clip_01.srt"} <= names
    assert "content-package/linkedin-posts.md" not in names  # rejected, so not exported
    manifest = json.loads(z.read("content-package/manifest.json"))
    assert manifest["published"] is False
    assert {a["id"] for a in manifest["assets"]} == {clip["id"], blog["id"]}
    clip_entry = next(a for a in manifest["assets"] if a["id"] == clip["id"])
    assert clip_entry["approved_by"] == "ana"
    assert len(clip_entry["files"]["landscape"]["sha256"]) == 64
    assert manifest["consent"].startswith("ana: speakers signed release")


def test_job_from_a_meeting_link_uses_that_meetings_recording(env):
    client, calls = env
    preview = client.get("/api/v1/meetstream/resolve", params={"link": "https://zoom.us/j/8812345678"}).json()
    assert preview["bot_id"] == "bot_42" and preview["resolved_from"] == "meeting_link"
    assert [b["bot_id"] for b in preview["other_matches"]] == ["bot_7"]  # older recording of the same meeting

    r = client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording", "link": "https://zoom.us/j/8812345678?pwd=zzz"},
                                       "configuration": CONFIG})
    assert r.status_code == 202
    assert r.json()["source"]["bot_id"] == "bot_42" and r.json()["source"]["resolved_from"] == "meeting_link"
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "awaiting_review", job["error_message"]
    assert job["webinar"]["source_ref"] == "bot_42"

    # A MeetStream URL carrying the id goes straight to that bot.
    r = client.get("/api/v1/meetstream/resolve",
                   params={"link": "https://app.meetstream.ai/bots/5b6c7d8e-1f2a-4b3c-9d4e-5f6a7b8c9d0e"})
    assert r.json()["resolved_from"] == "meetstream_url"

    # Links that lead nowhere are refused up front with a reason (no job is created).
    for bad, why in (("https://meet.google.com/zzz-zzzz-zzz", "No MeetStream bot has joined"),
                     ("https://www.youtube.com/watch?v=x", "Not a MeetStream link"),
                     ("2e3f4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b", "no recording with the id")):
        r = client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording", "link": bad}})
        assert r.status_code == 422 and why in r.json()["detail"]
    assert client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording"}}).status_code == 422


def test_review_rules_are_enforced_by_the_backend(env):
    client, _ = env
    r = client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording", "recording_id": "bot_42"}, "configuration": CONFIG})
    assert r.status_code == 202
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "awaiting_review", job["error_message"]
    assets = client.get(f"{P}/jobs/{job['job_id']}/assets").json()

    # Request changes needs feedback; then the editor revises; it goes back to the queue as v2.
    post = by_type(assets, "linkedin_post")[0]
    assert client.post(f"{P}/assets/{post['id']}/request-changes", json={}).status_code == 409
    assert client.post(f"{P}/assets/{post['id']}/request-changes", json={"feedback": "shorter"}).json()["status"] == "CHANGES_REQUESTED"
    assert client.post(f"{P}/assets/{post['id']}/approve", json={}).status_code == 409  # must be revised first
    edited = client.patch(f"{P}/assets/{post['id']}", json={"content": {"text": "Shorter post."}}).json()
    assert edited["status"] == "PENDING_REVIEW" and edited["version"] == 2
    assert [v["author"] for v in edited["versions"]] == ["offline", "reviewer"]
    assert [r["decision"] for r in edited["reviews"]][-3:] == ["request_changes", "edit", "submit"]

    # Editing an approved asset puts it back under review.
    client.post(f"{P}/assets/{post['id']}/approve", json={})
    again = client.patch(f"{P}/assets/{post['id']}", json={"content": {"text": "Changed after approval."}}).json()
    assert again["status"] == "PENDING_REVIEW"

    # A quote can't be "edited" into words nobody said.
    quote = by_type(assets, "quote")[0]
    r = client.patch(f"{P}/assets/{quote['id']}", json={"content": {"quote": "Agents are magic and never fail."}})
    assert r.status_code == 409 and "verbatim" in r.json()["detail"].lower()

    # Clip boundaries can be adjusted; the clip and captions are re-rendered.
    clip = by_type(assets, "clip")[0]
    start = clip["content"]["start"]
    moved = client.patch(f"{P}/assets/{clip['id']}", json={"content": {"start": start + 1.0, "end": start + 7.0}}).json()
    assert moved["version"] == 2 and moved["content"]["duration"] == pytest.approx(6.0)
    assert moved["source_references"][0]["start_seconds"] == pytest.approx(start + 1.0)
    assert client.patch(f"{P}/assets/{clip['id']}", json={"content": {"start": 5, "end": 9999}}).status_code == 409

    # Rejected assets can be reopened, not approved directly.
    t = by_type(assets, "takeaway")[0]
    client.post(f"{P}/assets/{t['id']}/reject", json={})
    assert client.post(f"{P}/assets/{t['id']}/approve", json={}).status_code == 409
    assert client.post(f"{P}/assets/{t['id']}/reopen", json={}).json()["status"] == "PENDING_REVIEW"


def test_meetstream_recording_without_audio_fails_clearly(tmp_path):
    import subprocess
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
                    "-pix_fmt", "yuv420p", str(silent)], check=True)
    settings = Settings(data_dir=tmp_path / "data", llm_mode="offline", meetstream_api_key="ms_key", job_workers=1)
    transport = fake_meetstream(silent.read_bytes(), [])
    factory = ContentFactory(settings, Store(settings.db_path),
                             meetstream_factory=lambda: MeetStreamClient("ms_key", transport=transport))
    with TestClient(create_app(settings, factory)) as client:
        r = client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording", "recording_id": "bot_42"}})
        job = wait(client, r.json()["job_id"])
        assert job["status"] == "failed" and "no audio" in job["error_message"]


def s3_link(bot_id: str, signed: datetime) -> str:
    return (f"https://example-bucket.s3.amazonaws.com/{bot_id}/meeting_recording.mp4"
            f"?X-Amz-Date={signed:%Y%m%dT%H%M%SZ}&X-Amz-Expires=3600&X-Amz-Signature=abc")


def test_job_from_a_meetstream_download_link(env):
    client, calls = env
    link = s3_link(OTHER_BOT, datetime.now(timezone.utc) - timedelta(minutes=5))
    r = client.get("/api/v1/meetstream/resolve", params={"link": link}).json()
    # The link names its bot, so MeetStream's speaker-labelled transcript is used instead of transcribing.
    assert (r["resolved_from"], r["bot_id"], r["transcript"]) == ("recording_url", OTHER_BOT, "meetstream")
    assert r["expires_at"] and r["size"] > 0

    r = client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording", "link": link}, "configuration": CONFIG})
    assert r.status_code == 202 and r.json()["source"]["type"] == "recording_url"
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "awaiting_review", job["error_message"]
    assert job["webinar"]["source_type"] == "recording_url" and job["webinar"]["title"] == "Meeting recording"
    assert f"/api/v1/transcript/tr_{OTHER_BOT}/get_transcript" in calls and not any(c.startswith("transcribe:") for c in calls)

    # An expired link is refused before anything starts.
    old = s3_link(OTHER_BOT, datetime.now(timezone.utc) - timedelta(hours=2))
    r = client.post(f"{P}/jobs", json={"source": {"type": "meetstream_recording", "link": old}})
    assert r.status_code == 422 and "expired" in r.json()["detail"]


def test_dropped_in_recording_is_transcribed_and_processed(env, tiny_video):
    client, calls = env
    r = client.post(f"{P}/uploads", data={"title": "", "configuration": json.dumps(CONFIG)},
                    files={"recording": ("q4-launch_webinar.mp4", tiny_video.read_bytes(), "video/mp4")})
    assert r.status_code == 202, r.text
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "awaiting_review", job["error_message"]
    assert job["webinar"]["source_type"] == "upload" and job["webinar"]["title"] == "Q4 launch webinar"
    assert "transcribe:recording.mp4" in calls
    assert len([a for a in client.get(f"{P}/jobs/{job['job_id']}/assets").json() if a["asset_type"] == "clip"]) >= 1
    bad = client.post(f"{P}/uploads", files={"recording": ("notes.pdf", b"%PDF", "application/pdf")})
    assert bad.status_code == 415


def test_without_meetstream_recording_links_still_work(tmp_path, tiny_video):
    settings = Settings(data_dir=tmp_path, llm_mode="offline", meetstream_api_key=None, job_workers=1)
    transport = fake_meetstream(tiny_video.read_bytes(), [])
    factory = ContentFactory(settings, Store(settings.db_path), transcriber=fake_transcriber([]), url_transport=transport)
    with TestClient(create_app(settings, factory)) as client:
        r = client.get("/api/v1/meetstream/resolve", params={"link": "https://zoom.us/j/8812345678"})
        assert r.status_code == 422 and "MeetStream isn't connected" in r.json()["detail"]
        link = s3_link(OTHER_BOT, datetime.now(timezone.utc))
        assert client.get("/api/v1/meetstream/resolve", params={"link": link}).json()["transcript"] == "local"


def test_upload_size_is_capped(tmp_path):
    settings = Settings(data_dir=tmp_path, llm_mode="offline", job_workers=1, max_upload_mb=1)
    with TestClient(create_app(settings)) as client:
        big = b"\0" * (1024 * 1024 + 1)
        r = client.post(f"{P}/uploads", files={"recording": ("big.mp4", big, "video/mp4")})
        assert r.status_code == 413 and "upload limit" in r.json()["detail"]
        assert not any((tmp_path / "uploads").glob("*/recording.mp4"))


@pytest.mark.parametrize("link", ["http://127.0.0.1:8080/rec.mp4", "http://169.254.169.254/latest/rec.mp4",
                                  "http://10.0.0.5/rec.mp4", "http://[::1]/rec.mp4", "http://100.64.0.7/rec.mp4",
                                  "http://[::ffff:127.0.0.1]/rec.mp4", "http://0.0.0.0/rec.mp4"])
def test_links_to_private_addresses_are_refused_without_a_token(tmp_path, monkeypatch, link):
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    settings = Settings(data_dir=tmp_path, llm_mode="offline", meetstream_api_key=None)
    hit = []
    transport = httpx.MockTransport(lambda req: hit.append(req) or httpx.Response(200, content=b"x"))
    factory = ContentFactory(settings, Store(settings.db_path), url_transport=transport)
    with TestClient(create_app(settings, factory)) as client:
        r = client.get("/api/v1/meetstream/resolve", params={"link": link})
        assert r.status_code == 422 and "private or local" in r.json()["detail"]
    assert hit == []


def test_redirects_to_private_addresses_are_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    settings = Settings(data_dir=tmp_path, llm_mode="offline", meetstream_api_key=None)
    def handler(req):
        if req.url.host == "93.184.215.14":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/rec.mp4"})
        return httpx.Response(206, content=b"x", headers={"content-type": "video/mp4"})
    factory = ContentFactory(settings, Store(settings.db_path), url_transport=httpx.MockTransport(handler))
    with TestClient(create_app(settings, factory)) as client:
        r = client.get("/api/v1/meetstream/resolve", params={"link": "http://93.184.215.14/rec.mp4"})
        assert r.status_code == 422 and "127.0.0.1" in r.json()["detail"]


def test_private_addresses_are_allowed_behind_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_API_TOKEN", "s3cret")
    settings = Settings(data_dir=tmp_path, llm_mode="offline", meetstream_api_key=None)
    transport = httpx.MockTransport(lambda req: httpx.Response(206, content=b"x", headers={"content-type": "video/mp4"}))
    factory = ContentFactory(settings, Store(settings.db_path), url_transport=transport)
    with TestClient(create_app(settings, factory)) as client:
        r = client.get("/api/v1/meetstream/resolve", params={"link": "http://127.0.0.1/rec.mp4"},
                       headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200


def test_api_token_is_required_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_API_TOKEN", "s3cret")
    settings = Settings(data_dir=tmp_path, llm_mode="offline")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health").json()["auth_required"] is True
        assert client.get(f"{P}/jobs").status_code == 401
        assert client.get(f"{P}/jobs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get(f"{P}/jobs?access_token=s3cret").status_code == 200


def test_keys_can_be_set_from_the_dashboard(tmp_path, monkeypatch):
    from fastapi import HTTPException

    import contentfactory.api as api_module
    from contentfactory.generators import OpenAIGenerator

    for var in ("OPENAI_API_KEY", "CF_LLM", "MEETSTREAM_API_KEY", "CF_MODEL"):
        monkeypatch.delenv(var, raising=False)
    checked = []

    def check_openai(key, model):
        checked.append((key, model))
        if key != "sk-good-key-1234":
            raise HTTPException(422, "OpenAI didn't accept that key.")

    monkeypatch.setattr(api_module, "check_openai_key", check_openai)
    monkeypatch.setattr(api_module, "check_meetstream_key", lambda key, base: None)
    settings = Settings(data_dir=tmp_path)
    factory = ContentFactory(settings, Store(settings.db_path))
    with TestClient(create_app(settings, factory)) as client:
        s = client.get("/api/v1/settings").json()
        assert s["openai_api_key"]["set"] is False and s["llm_mode"] == "offline"

        # A key OpenAI rejects is not saved.
        r = client.post("/api/v1/settings", json={"openai_api_key": "sk-typo"})
        assert r.status_code == 422 and "didn't accept" in r.json()["detail"]
        assert not settings.secrets_path.exists()

        r = client.post("/api/v1/settings", json={"openai_api_key": "sk-good-key-1234", "meetstream_api_key": "ms_abcdefgh5678"})
        assert r.status_code == 200
        body = r.json()
        assert "sk-good-key-1234" not in r.text and body["openai_api_key"] == {"set": True, "source": "saved", "hint": "…1234"}
        assert body["meetstream_api_key"]["hint"] == "…5678"
        assert client.get("/api/v1/health").json()["llm_mode"] == "openai"
        assert oct(settings.secrets_path.stat().st_mode & 0o777) == "0o600"
        # The next job uses the new key.
        gen = factory.generator
        assert isinstance(gen, OpenAIGenerator) and gen.client.api_key == "sk-good-key-1234"

        # Changing only the model re-checks the saved key against it.
        client.post("/api/v1/settings", json={"openai_model": "gpt-5"})
        assert checked[-1] == ("sk-good-key-1234", "gpt-5") and factory.generator.model == "gpt-5"

        # Removing the key goes back to offline, and survives a restart.
        assert client.post("/api/v1/settings", json={"openai_api_key": ""}).json()["llm_mode"] == "offline"
    restarted = Settings(data_dir=tmp_path)
    restarted.apply_saved()
    assert restarted.openai_api_key is None and restarted.model == "gpt-5" and restarted.meetstream_api_key == "ms_abcdefgh5678"
