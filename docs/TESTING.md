# Testing

```bash
.venv/bin/python -m pytest            # everything, ~20 s
.venv/bin/python -m pytest -q -k quote   # by name
```

The suite needs FFmpeg (the end-to-end tests render real clips). It needs **no network and no API keys**: OpenAI and MeetStream are mocked at the HTTP layer, and local transcription is replaced by a fake (`fake_transcriber`), so no Whisper model is downloaded.

## What's covered

| File | Tests |
|---|---|
| `tests/test_ingest.py` | MeetStream segment-list and participant-keyed formats, transcripts without word timings, non-JSON and empty transcripts, caption cue timing and format |
| `tests/test_validate.py` | Quote verification (exact, spanning segments, paraphrase rejected, wrong citation corrected), clip snapping/padding/scoring, topic-change and length trimming, unknown ids, overlap and score filtering, citation resolution, sensitive-content flags, chapter boundaries on the sample, the review state machine |
| `tests/test_clients.py` | OpenAI request shape (Responses API, model, reasoning effort, strict structured output schema, transcript in the cached instructions, `prompt_cache_key`, `store: false`, no sampling params), structured parsing, refusal and truncation → readable errors, auth error; MeetStream `Token` auth, 202 handling, bot summaries; resolving links (meeting-link normalisation across Zoom/Meet/Teams variants, MeetStream URLs, newest finished recording wins, live, denied and unmatched meetings, pagination that stops if the cursor is ignored) |
| `tests/test_api.py` | Full runs through the HTTP API with a fake MeetStream: real FFmpeg renders, a job started from a Zoom link (and links refused with a reason), every asset type, the review rules, edits re-rendering clips, verbatim enforcement, consent and approval gates, ZIP contents and manifest; a recording with no audio; a job from a MeetStream download link (using MeetStream's transcript) and an expired one refused; a dropped-in file transcribed and processed; recording links without a MeetStream key; API token auth |

## Fixtures

`tests/conftest.py` provides:

- **`ms_transcript_json` / `transcript`**: a 10-sentence, two-speaker transcript in MeetStream format, with word timings. It contains:
  - a strong hook (`s0003`);
  - a spoken topic change (`s0006`, "Moving on to…");
  - a quotable line (`s0007`);
  - a "confidential" line (`s0009`).
- **`tiny_video`**: 45 s of FFmpeg test pattern with a tone, built once per session.

`tests/test_api.py` builds the app with `create_app(Settings(data_dir=tmp_path, llm_mode="offline", …), factory)`. It injects a `MeetStreamClient` whose `httpx.MockTransport` plays MeetStream, including the presigned S3 download. It also checks that our API key is **not** sent to S3.

## Mocking the OpenAI SDK

The OpenAI Python SDK 3.x is built on **`httpx2`**, not `httpx`, so mock transports must come from that package:

```python
import openai, httpx2

def handler(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)            # inspect what we sent
    return httpx2.Response(200, json={...a Responses API body...})

client = openai.OpenAI(api_key="test", max_retries=0,
                       http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
gen = OpenAIGenerator(client=client)
```

## Adding tests

- **A new MeetStream transcript shape:** add a case to `test_ingest.py` with a small inline sample, and assert the segment texts, speakers and times.
- **A new QA rule:** add it to `validate.py`, and test it in `test_validate.py` against the `transcript` fixture.
- **A new asset type or stage:** extend `test_meetstream_recording_to_exported_package` to assert it's created, reviewable and exported.
- **A prompt change:** `test_openai_request_shape_and_structured_parse` should still pass. Prompt *quality* isn't unit-tested; judge it on real recordings.

## Manual checks worth doing

Unit tests can't judge these:

1. Run the sample with OpenAI (`OPENAI_API_KEY` set) and read the blog. Are the claims in each paragraph actually in the cited moment? Click the chips to check.
2. Watch the portrait clips on a phone. Are the captions readable, and is the slide content still visible?
3. Try a real hour-long MeetStream recording. Do the chapter boundaries match the agenda?
