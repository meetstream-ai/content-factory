"""Wire-level checks for the two external clients, against mocked HTTP."""

import json

import httpx
import httpx2  # the OpenAI SDK 3.x HTTP stack
import openai
import pytest

from contentfactory.drafts import ChapterNames, JobConfig
from contentfactory.generators import Chapter, GenContext, GenerationError, OpenAIGenerator
from contentfactory.meetstream import (
    MeetStreamClient, MeetStreamError, MeetStreamNotReady, RecordingLookupError, classify_reference, normalize_meeting_link,
    resolve_recording, summarize_bot,
)


def openai_with(handler) -> OpenAIGenerator:
    client = openai.OpenAI(api_key="test", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return OpenAIGenerator(model="gpt-5-mini", effort="medium", client=client)


def response(content: list[dict], status: str = "completed", incomplete_reason: str | None = None) -> dict:
    """A Responses API body with one assistant message."""
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-5-mini", "status": status,
            "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
            "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": status, "content": content}],
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}


def text(t: str) -> dict:
    return {"type": "output_text", "text": t, "annotations": []}


def ctx(transcript) -> GenContext:
    return GenContext(title="Meeting Agents", transcript=transcript, config=JobConfig(),
                      chapters=[Chapter(index=0, first=0, last=4), Chapter(index=1, first=5, last=9)])


def test_openai_request_shape_and_structured_parse(transcript):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        out = {"chapters": [{"index": 0, "title": "Why Tools Aren't Agents", "summary": "s", "keywords": ["agents"]},
                            {"index": 1, "title": "Failing Gracefully", "summary": "s", "keywords": ["retries"]}]}
        return httpx2.Response(200, json=response([text(json.dumps(out))]))

    names = openai_with(handler).name_chapters(ctx(transcript))
    assert isinstance(names, ChapterNames) and names.chapters[1].title == "Failing Gracefully"

    body = seen["body"]
    assert seen["path"] == "/v1/responses"
    assert body["model"] == "gpt-5-mini"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] is True
    assert body["store"] is False
    assert "temperature" not in body
    # The transcript is in the instructions: the prefix every stage shares, so it is cached.
    assert "[s0007 " in body["instructions"] and "Dev Patel:" in body["instructions"]
    assert body["prompt_cache_key"] == "contentfactory-web_test"
    # Chapter ranges are given to the model, not left for it to invent.
    assert "s0001..s0005" in body["input"]


def test_openai_refusal_is_a_generation_error(transcript):
    def handler(request):
        return httpx2.Response(200, json=response([{"type": "refusal", "refusal": "I can't help with that."}]))

    with pytest.raises(GenerationError, match="declined"):
        openai_with(handler).name_chapters(ctx(transcript))


def test_openai_truncation_is_a_generation_error(transcript):
    def handler(request):
        return httpx2.Response(200, json=response([text('{"chapters": [{"ind')], status="incomplete",
                                                  incomplete_reason="max_output_tokens"))

    with pytest.raises(GenerationError, match="max_output_tokens"):
        openai_with(handler).name_chapters(ctx(transcript))


def test_openai_auth_error_is_explained(transcript):
    def handler(request):
        return httpx2.Response(401, json={"error": {"message": "bad key", "type": "invalid_request_error", "code": "invalid_api_key"}})

    with pytest.raises(GenerationError, match="credentials"):
        openai_with(handler).name_chapters(ctx(transcript))


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


# ---------------------------------------------------------------- resolving MeetStream links

UUID = "5b6c7d8e-1f2a-4b3c-9d4e-5f6a7b8c9d0e"


@pytest.mark.parametrize("a,b", [
    ("https://zoom.us/j/1234567890?pwd=abc", "https://us02web.zoom.us/j/1234567890"),
    ("https://meet.google.com/abc-defg-hij", "HTTPS://Meet.Google.com/abc-defg-hij/?authuser=1"),
    ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_X%40thread.v2/0?context=1",
     "https://teams.microsoft.com/l/meetup-join/19:meeting_X@thread.v2/0"),
])
def test_meeting_links_normalize_to_the_same_key(a, b):
    assert normalize_meeting_link(a) == normalize_meeting_link(b) is not None


def test_classify_reference():
    assert classify_reference(UUID) == ("bot_id", UUID)
    assert classify_reference(f"https://app.meetstream.ai/bots/{UUID}?tab=video") == ("meetstream_url", UUID)
    assert classify_reference(f"https://api.meetstream.ai/api/v1/bots/{UUID}/detail")[1] == UUID
    assert classify_reference("https://meet.google.com/abc-defg-hij")[0] == "meeting_link"
    with pytest.raises(RecordingLookupError, match="doesn't contain a bot id"):
        classify_reference("https://app.meetstream.ai/dashboard")
    with pytest.raises(RecordingLookupError, match="Not a MeetStream link"):
        classify_reference("https://www.youtube.com/watch?v=x")


def bots_client(pages: list[dict], calls: list | None = None) -> MeetStreamClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(dict(request.url.params))
        i = min(len(calls or [None]) - 1, len(pages) - 1) if calls is not None else 0
        return httpx.Response(200, json=pages[i])
    return MeetStreamClient("k", transport=httpx.MockTransport(handler))


LINK = "https://meet.google.com/abc-defg-hij"


def bot(bot_id, status, created, link=LINK):
    return {"bot_id": bot_id, "status": status, "meeting_link": link, "created_at": created}


def test_meeting_link_resolves_to_newest_finished_recording():
    page = {"bots": [bot("old", "Stopped", "2026-10-01T10:00:00Z"),
                     bot("new", "Done", "2026-10-20T10:00:00Z"),
                     bot("live", "InMeeting", "2026-10-27T10:00:00Z"),
                     bot("other", "Done", "2026-10-25T10:00:00Z", link="https://zoom.us/j/999")],
            "hasNextPage": False}
    r = resolve_recording(bots_client([page]), LINK + "?authuser=0")
    assert (r.bot_id, r.resolved_from) == ("new", "meeting_link")
    assert {b["bot_id"] for b in r.other_matches} == {"old", "live"}


def test_meeting_link_with_only_a_live_bot_or_a_denied_bot():
    live = {"bots": [bot("b", "InMeeting", "2026-10-27T10:00:00Z")], "hasNextPage": False}
    with pytest.raises(RecordingLookupError, match="still in that meeting"):
        resolve_recording(bots_client([live]), LINK)
    denied = {"bots": [bot("b", "NotAllowed", "2026-10-27T10:00:00Z")], "hasNextPage": False}
    with pytest.raises(RecordingLookupError, match="didn't record it"):
        resolve_recording(bots_client([denied]), LINK)
    with pytest.raises(RecordingLookupError, match="No MeetStream bot has joined"):
        resolve_recording(bots_client([{"bots": [], "hasNextPage": False}]), LINK)


def test_meeting_link_search_follows_pages_and_stops_if_cursor_is_ignored():
    calls: list = []
    p1 = {"bots": [bot("a", "Done", "2026-10-01", link="https://zoom.us/j/1")], "hasNextPage": True, "nextCursor": "c1"}
    p2 = {"bots": [bot("b", "Done", "2026-10-02")], "hasNextPage": True, "nextCursor": "c2"}
    r = resolve_recording(bots_client([p1, p2, p2], calls), LINK)
    assert r.bot_id == "b"
    assert calls[1].get("cursor") == "c1"
    assert len(calls) == 3  # third call returned page 2 again (no new bots), so it stopped


def test_meeting_link_falls_back_to_bot_detail_when_list_omits_links():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/bots":
            return httpx.Response(200, json={"bots": [{"BotID": "x1", "Status": "Done"}, {"BotID": "x2", "Status": "Done"}],
                                             "hasNextPage": False})
        bot_id = request.url.path.split("/")[-2]
        link = LINK if bot_id == "x2" else "https://zoom.us/j/1"
        return httpx.Response(200, json={"bot_details": {"BotID": bot_id, "MeetingLink": link, "Status": "Stopped",
                                                         "CreatedAt": "2026-10-20T10:00:00Z"}})

    r = resolve_recording(MeetStreamClient("k", transport=httpx.MockTransport(handler)), LINK)
    assert r.bot_id == "x2"


def test_bot_id_and_meetstream_url_are_checked_with_meetstream():
    statuses = {UUID: "Stopped", "live-bot": "InMeeting", "denied-bot": "Denied"}

    def handler(request):
        bot_id = request.url.path.split("/")[-2]
        assert request.url.path.endswith("/detail")
        if bot_id == "other-account":
            return httpx.Response(403, json={"message": "forbidden"})
        if bot_id not in statuses:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"bot_details": {"BotID": bot_id, "Status": statuses[bot_id], "Platform": "Zoom"}})

    client = MeetStreamClient("k", transport=httpx.MockTransport(handler))
    r = resolve_recording(client, UUID)
    assert (r.bot_id, r.resolved_from, r.bot["platform"]) == (UUID, "bot_id", "Zoom")
    assert resolve_recording(client, f"https://app.meetstream.ai/bots/{UUID}").resolved_from == "meetstream_url"
    # Problems are reported before any job exists, in words the user can act on.
    for ref, why in (("2e3f4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b", "no recording with the id"),
                     ("other-account", "different MeetStream account"),
                     ("live-bot", "still in that meeting"), ("denied-bot", "didn't record it")):
        with pytest.raises(RecordingLookupError, match=why):
            resolve_recording(client, ref)


def test_bot_ids_are_url_encoded_in_paths():
    seen = []
    transport = httpx.MockTransport(lambda req: seen.append(req.url.raw_path) or httpx.Response(200, json={"bot_details": {}}))
    MeetStreamClient("k", base_url="https://api.test/api/v1", transport=transport).get_bot_detail("abc#?x=1")
    assert seen == [b"/api/v1/bots/abc%23%3Fx%3D1/detail"]


def test_bot_transcript_is_fetched_by_transcript_id():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path.endswith("/detail"):
            return httpx.Response(200, json={"bot_details": {"BotID": "b1", "transcript_id": "tr_1"}})
        return httpx.Response(200, json=[{"speaker": "Ana", "transcript": "Hi."}])

    client = MeetStreamClient("k", base_url="https://api.test/api/v1", transport=httpx.MockTransport(handler))
    assert client.get_bot_transcript("b1") == [{"speaker": "Ana", "transcript": "Hi."}]
    assert seen == ["/api/v1/bots/b1/detail", "/api/v1/transcript/tr_1/get_transcript"]


def test_bot_without_a_transcript_id():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"bot_details": {"BotID": "b1"}}))
    with pytest.raises(MeetStreamError) as excinfo:
        MeetStreamClient("k", base_url="https://api.test/api/v1", transport=transport).get_bot_transcript("b1")
    assert excinfo.value.status == 409
