# Testing

```bash
.venv/bin/python -m pytest            # everything, ~20 s
.venv/bin/python -m pytest -q -k quote   # by name
```

The suite needs FFmpeg (the end-to-end tests render real clips). It needs **no network and no API keys**: Anthropic and MeetStream are mocked at the HTTP layer.

## What's covered

| File | Tests |
|---|---|
| `tests/test_ingest.py` | MeetStream segment-list and participant-keyed formats, SRT with speaker prefixes, VTT voice tags, empty transcripts, caption cue timing and format |
| `tests/test_validate.py` | Quote verification (exact, spanning segments, paraphrase rejected, wrong citation corrected), clip snapping/padding/scoring, topic-change and length trimming, unknown ids, overlap and score filtering, citation resolution, sensitive-content flags, chapter boundaries on the sample, the review state machine |
| `tests/test_clients.py` | Claude request shape (model, adaptive thinking, effort, structured output schema, prompt caching on the transcript, refusal fallback, no sampling params), structured parsing, refusal → readable error, auth error; MeetStream `Token` auth, 202 handling, bot summaries |
| `tests/test_api.py` | Full runs through the HTTP API, with a fake MeetStream bot and an upload: real FFmpeg renders, every asset type, the review rules, edits re-rendering clips, verbatim enforcement, consent and approval gates, ZIP contents and manifest; an upload with no audio; bad file types; API token auth |

## Fixtures

`tests/conftest.py` provides:

- **`ms_transcript_json` / `transcript`**: a 10-sentence, two-speaker transcript in MeetStream format, with word timings. It contains:
  - a strong hook (`s0003`);
  - a spoken topic change (`s0006`, "Moving on to…");
  - a quotable line (`s0007`);
  - a "confidential" line (`s0009`).
- **`tiny_video`**: 45 s of FFmpeg test pattern with a tone, built once per session.

`tests/test_api.py` builds the app with `create_app(Settings(data_dir=tmp_path, llm_mode="offline", …), factory)`. It injects a `MeetStreamClient` whose `httpx.MockTransport` plays MeetStream, including the presigned S3 download. It also checks that our API key is **not** sent to S3.

## Mocking the Anthropic SDK

The Anthropic Python SDK 2.x is built on **`httpx2`**, not `httpx`, so mock transports must come from that package:

```python
import anthropic, httpx2

def handler(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)            # inspect what we sent
    return httpx2.Response(200, json={...a Messages API response...})

client = anthropic.Anthropic(api_key="test", max_retries=0,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
gen = ClaudeGenerator(client=client)
```

## Adding tests

- **A new transcript format:** add a case to `test_ingest.py` with a small inline sample, and assert the segment texts, speakers and times.
- **A new QA rule:** add it to `validate.py`, and test it in `test_validate.py` against the `transcript` fixture.
- **A new asset type or stage:** extend `test_meetstream_recording_to_exported_package` to assert it's created, reviewable and exported.
- **A prompt change:** `test_claude_request_shape_and_structured_parse` should still pass. Prompt *quality* isn't unit-tested; judge it on real recordings.

## Manual checks worth doing

Unit tests can't judge these:

1. Run the sample with Claude (`ANTHROPIC_API_KEY` set) and read the blog. Are the claims in each paragraph actually in the cited moment? Click the chips to check.
2. Watch the portrait clips on a phone. Are the captions readable, and is the slide content still visible?
3. Try a real hour-long MeetStream recording. Do the chapter boundaries match the agenda?
