"""Wire-level checks for the two external clients, against mocked HTTP."""

import json

import anthropic
import httpx
import httpx2  # the Anthropic SDK 2.x HTTP stack
import pytest

from contentfactory.drafts import ChapterNames, JobConfig
from contentfactory.generators import Chapter, ClaudeGenerator, GenContext, GenerationError
from contentfactory.meetstream import MeetStreamClient, MeetStreamNotReady, summarize_bot


def claude_with(handler) -> ClaudeGenerator:
    client = anthropic.Anthropic(api_key="test", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return ClaudeGenerator(model="claude-opus-5-5", effort="medium", client=client)


def message(text: str, stop_reason: str = "end_turn", **extra) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}, **extra}


def ctx(transcript) -> GenContext:
    return GenContext(title="Meeting Agents", transcript=transcript, config=JobConfig(),
                      chapters=[Chapter(index=0, first=0, last=4), Chapter(index=1, first=5, last=9)])


def test_claude_request_shape_and_structured_parse(transcript):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        out = {"chapters": [{"index": 0, "title": "Why Tools Aren't Agents", "summary": "s", "keywords": ["agents"]},
                            {"index": 1, "title": "Failing Gracefully", "summary": "s", "keywords": ["retries"]}]}
        return httpx2.Response(200, json=message(json.dumps(out)))

    names = claude_with(handler).name_chapters(ctx(transcript))
    assert isinstance(names, ChapterNames) and names.chapters[1].title == "Failing Gracefully"

    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert "temperature" not in body
    # The transcript is the cached prefix shared by every stage.
    system = body["system"]
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "[s0007 " in system[1]["text"] and "Dev Patel:" in system[1]["text"]
    # Chapter ranges are given to the model, not left for it to invent.
    assert "s0001..s0005" in body["messages"][0]["content"]


def test_claude_refusal_is_a_generation_error(transcript):
    def handler(request):
        return httpx2.Response(200, json=message("", stop_reason="refusal",
                                                stop_details={"type": "refusal", "category": "cyber", "explanation": None}))

    with pytest.raises(GenerationError, match="declined"):
        claude_with(handler).name_chapters(ctx(transcript))


def test_claude_auth_error_is_explained(transcript):
    def handler(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})

    with pytest.raises(GenerationError, match="credentials"):
        claude_with(handler).name_chapters(ctx(transcript))


def test_meetstream_uses_token_auth_and_reports_processing():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/get_video"):
            return httpx.Response(202, json={"message": "processing"})
        if request.url.path == "/api/v1/bots":
            return httpx.Response(200, json={"bots": [{"BotID": "b1", "Status": "Stopped", "Platform": "Zoom"}],
                                             "hasNextPage": False})
        return httpx.Response(404, json={})

    client = MeetStreamClient("key123", transport=httpx.MockTransport(handler))
    bots = client.list_bots(platform="Zoom")["bots"]
    assert summarize_bot(bots[0])["bot_id"] == "b1"
    with pytest.raises(MeetStreamNotReady):
        client.get_video("b1")
    assert calls[0] == ("/api/v1/bots", "Token key123")
