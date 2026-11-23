"""A local stand-in for MeetStream's REST API, for trying the app without a MeetStream account.

The Content Factory only takes recordings and transcripts from MeetStream. This script plays
MeetStream on localhost so the whole flow can be demoed: it serves the sample webinar
(`scripts/make_sample.py`) as a finished bot recording, plus a few other bots that show how
meeting links are resolved.

    .venv/bin/python scripts/make_sample.py samples            # once: builds samples/sample_webinar.mp4
    .venv/bin/uvicorn --app-dir scripts demo_meetstream:app --port 8765
    MEETSTREAM_API_KEY=demo MEETSTREAM_BASE_URL=http://127.0.0.1:8765/api/v1 \
        .venv/bin/uvicorn contentfactory.api:app

Then paste one of these into the dashboard:
    https://zoom.us/j/8812345678            recorded twice; the newest recording is used
    https://meet.google.com/abc-defg-hij    the bot is still in the meeting (refused, with the reason)
    https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC%40thread.v2/0   bot was denied entry

Like the real List Bots endpoint may, the list omits meeting links, so the app reads them
from each bot's detail. Only the routes the app calls are implemented.
"""

import json
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

SAMPLES = Path(os.environ.get("CF_SAMPLES_DIR", Path(__file__).resolve().parent.parent / "samples"))
API_KEY = os.environ.get("DEMO_MEETSTREAM_KEY", "demo")

app = FastAPI(title="MeetStream stand-in")

# Every bot serves the same sample recording; they differ in meeting link and status.
BOTS = {
    "3f2b9c1e-6a4d-4e8f-9b7a-1c2d3e4f5a6b": {"status": "Stopped", "platform": "Zoom", "created_at": "2026-10-28T16:00:00Z",
                                             "link": "https://us02web.zoom.us/j/8812345678?pwd=Xy12"},
    "5b6c7d8e-1f2a-4b3c-9d4e-5f6a7b8c9d0e": {"status": "Stopped", "platform": "Zoom", "created_at": "2026-10-21T16:00:00Z",
                                             "link": "https://zoom.us/j/8812345678"},
    "7d1e2f3a-4b5c-4d6e-8f9a-0b1c2d3e4f5a": {"status": "InMeeting", "platform": "GMeet", "created_at": "2026-11-03T09:00:00Z",
                                             "link": "https://meet.google.com/abc-defg-hij"},
    "9a8b7c6d-5e4f-4a3b-2c1d-0e9f8a7b6c5d": {"status": "Denied", "platform": "Teams", "created_at": "2026-11-01T14:00:00Z",
                                             "link": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC%40thread.v2/0"},
}


def _bot(bot_id: str, authorization: str | None) -> dict:
    if authorization != f"Token {API_KEY}":
        raise HTTPException(403, "Invalid API key")
    if bot_id not in BOTS:
        raise HTTPException(404, "Bot not found")
    return BOTS[bot_id]


@app.get("/api/v1/bots")
def list_bots(authorization: str | None = Header(None)):
    if authorization != f"Token {API_KEY}":
        raise HTTPException(403, "Invalid API key")
    return {"bots": [{"bot_id": k, "bot_name": "Content bot", "status": b["status"], "platform": b["platform"],
                      "created_at": b["created_at"]} for k, b in BOTS.items()], "hasNextPage": False}


@app.get("/api/v1/bots/{bot_id}/detail")
def bot_detail(bot_id: str, authorization: str | None = Header(None)):
    b = _bot(bot_id, authorization)
    return {"bot_details": {"BotID": bot_id, "MeetingLink": b["link"], "Status": b["status"],
                            "Platform": b["platform"], "CreatedAt": b["created_at"], "transcript_id": f"tr-{bot_id}"}}


@app.get("/api/v1/transcript/{transcript_id}/get_transcript")
def get_transcript(transcript_id: str, authorization: str | None = Header(None)):
    _bot(transcript_id.removeprefix("tr-"), authorization)
    return json.loads((SAMPLES / "sample_transcript.json").read_text())


@app.get("/api/v1/bots/{bot_id}/get_video")
def get_video(bot_id: str, request: Request, authorization: str | None = Header(None)):
    _bot(bot_id, authorization)
    if not (SAMPLES / "sample_webinar.mp4").exists():
        raise HTTPException(404, "Run scripts/make_sample.py first")
    # Real MeetStream returns a presigned S3 URL; this one stands in for it.
    return {"video_url": str(request.url_for("presigned", name=f"{bot_id}.mp4")) + "?X-Amz-Signature=demo", "video_info": {}}


@app.get("/s3/{name}", name="presigned")
def presigned(name: str):
    return FileResponse(SAMPLES / "sample_webinar.mp4", media_type="video/mp4")
