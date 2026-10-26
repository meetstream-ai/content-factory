"""End to end through the HTTP API: ingest, generate (offline), render with FFmpeg, review, export."""

import io
import json
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from contentfactory.api import create_app
from contentfactory.config import Settings
from contentfactory.db import Store
from contentfactory.meetstream import MeetStreamClient
from contentfactory.pipeline import ContentFactory

from .conftest import make_meetstream_transcript

P = "/api/v1/content-factory"
CONFIG = {"clip_count": 3, "min_clip_seconds": 5, "max_clip_seconds": 20, "clip_formats": ["landscape", "portrait"]}


def fake_meetstream(video_bytes: bytes, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if request.url.host == "ms-s3-bucket.s3.amazonaws.com":
            assert "authorization" not in request.headers  # presigned URLs carry their own auth
            return httpx.Response(200, content=video_bytes)
        assert request.headers["authorization"] == "Token ms_key"
        if path == "/api/v1/bots/bot_42/detail":
            return httpx.Response(200, json={"bot_details": {"BotID": "bot_42", "Platform": "Zoom", "CreatedAt": "2026-10-21T15:00:00Z"}})
        if path == "/api/v1/bots/bot_42/get_transcript":
            return httpx.Response(200, json=make_meetstream_transcript())
        if path == "/api/v1/bots/bot_42/get_video":
            return httpx.Response(200, json={"video_url": "https://ms-s3-bucket.s3.amazonaws.com/rec.mp4", "video_info": {}})
        if path == "/api/v1/bots":
            return httpx.Response(200, json={"bots": [{"bot_id": "bot_42", "status": "Stopped", "platform": "Zoom"}]})
        return httpx.Response(404, json={"message": "not found"})
    return httpx.MockTransport(handler)


@pytest.fixture
def env(tmp_path, tiny_video):
    calls: list[str] = []
    settings = Settings(data_dir=tmp_path, llm_mode="offline", meetstream_api_key="ms_key", job_workers=1)
    transport = fake_meetstream(tiny_video.read_bytes(), calls)
    factory = ContentFactory(settings, Store(settings.db_path),
                             meetstream_factory=lambda: MeetStreamClient("ms_key", transport=transport))
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
    assert "/api/v1/bots/bot_42/get_transcript" in calls
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


def test_review_rules_are_enforced_by_the_backend(env, tiny_video, ms_transcript_json):
    client, _ = env
    r = client.post(f"{P}/uploads", data={"title": "Upload test", "configuration": json.dumps(CONFIG)},
                    files={"recording": ("talk.mp4", tiny_video.read_bytes(), "video/mp4"),
                           "transcript": ("talk.json", ms_transcript_json, "application/json")})
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


def test_upload_without_audio_or_transcript_fails_clearly(env, tmp_path):
    client, _ = env
    import subprocess
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
                    "-pix_fmt", "yuv420p", str(silent)], check=True)
    r = client.post(f"{P}/uploads", data={"title": "Silent"}, files={"recording": ("s.mp4", silent.read_bytes(), "video/mp4")})
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "failed" and "no audio" in job["error_message"]
    assert client.post(f"{P}/uploads", data={"title": "x"}, files={"recording": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415


def test_api_token_is_required_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_API_TOKEN", "s3cret")
    settings = Settings(data_dir=tmp_path, llm_mode="offline")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health").json()["auth_required"] is True
        assert client.get(f"{P}/jobs").status_code == 401
        assert client.get(f"{P}/jobs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get(f"{P}/jobs?access_token=s3cret").status_code == 200
