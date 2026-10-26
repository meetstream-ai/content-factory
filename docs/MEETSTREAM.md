# MeetStream integration

[MeetStream](https://docs.meetstream.ai) sends bots into Zoom, Google Meet and Microsoft Teams calls to record and transcribe them. The Content Factory takes a finished bot's recording and transcript and turns them into a content package.

In MeetStream terms, **a bot is one recorded meeting**, so in this app a *recording id* is a MeetStream *bot id*.

## Setup

1. Create an API key at https://app.meetstream.ai/api-key.
2. Start the server with it:
   ```bash
   export MEETSTREAM_API_KEY=ms_...
   .venv/bin/uvicorn contentfactory.api:app
   ```
3. Check: `curl localhost:8000/api/v1/health` should show `"meetstream_configured": true`.

## What the app calls

All calls go to `https://api.meetstream.ai/api/v1` (override with `MEETSTREAM_BASE_URL`) with `Authorization: Token <key>`. Note the literal word `Token`: the REST API rejects `Bearer`.

| When | Call | MeetStream MCP equivalent | Used for |
|---|---|---|---|
| "Load recordings" in the dashboard | `GET /bots?status=&platform=&from=&to=` | `list_bots` | Picking a bot |
| Ingest step 1 | `GET /bots/{bot_id}/detail` | `get_bot_detail` | Platform and date for the default title |
| Ingest step 2 | `GET /bots/{bot_id}/get_transcript` | `get_transcript` | The speaker-labelled transcript |
| Ingest step 3 | `GET /bots/{bot_id}/get_video` | `get_media_urls` (`kind: video`) | Presigned MP4 URL |
| Fallback | `GET /bots/{bot_id}/get_audio` | `get_media_urls` (`kind: audio`) | Presigned WAV URL, for audio-only bots |
| Download | `GET <presigned S3 URL>` | | Sent **without** our auth header |

The client is `contentfactory/meetstream.py`. All calls are read-only. The app never creates, removes or deletes bots.

### Why REST and not MeetStream's MCP tools?

MeetStream's MCP server wraps these same endpoints as tools for AI agents. Inside the pipeline we call REST directly:
- **Exactness:** media has to be downloaded byte for byte, and transcripts parsed with their timings intact.
- **Cost and speed:** moving files through a model's context would cost tokens and add nothing.
- **Testability:** the REST calls are mocked in tests (`tests/test_api.py` runs a full fake bot).

Agents still get the MCP experience end to end: they use MeetStream's MCP to *find* a meeting, then this app's own MCP server to *process* it. See [MCP.md](MCP.md).

## Behaviour details

**Still processing (HTTP 202).** Transcript and media endpoints answer 202 while the bot is in the meeting or post-call processing is running. The app waits 15 s and retries, up to 10 attempts (about 2¼ minutes). If it's still not ready, the job fails with *"still processing"*; retry it later. Processing for long meetings can take several minutes after the call ends.

**Presigned links expire after about an hour**, so the app downloads immediately and never stores the URL. Downloads go to `<file>.part` first, then are renamed, so a failed download never leaves a truncated recording.

**Audio-only bots.** If `get_video` fails or returns no URL, the app uses `get_audio`. The job still produces chapters, the brief, blog, posts, quotes and design briefs. Clips become **audio excerpts (`.m4a`) with captions**, and the job carries a `no_video` warning.

**Errors are translated** into messages that say what to check:

| Status | Message |
|---|---|
| 403 | forbidden: check `MEETSTREAM_API_KEY`, or the bot belongs to another account |
| 404 | not found |
| 409 | no transcript: transcription was not enabled, or the bot was never admitted |
| other ≥400 | the status code and the start of the body |

**Raw responses are kept.** `data/webinars/<id>/meetstream_bot.json` and `meetstream_transcript.json` hold exactly what MeetStream returned, which helps when debugging a parse problem.

## Transcript formats

MeetStream returns one of two shapes, depending on the transcription provider. Both are handled:

**Segment-list** (e.g. Deepgram, or the MeetStream engine):

```json
[{"speaker": "Maddy Singh", "transcript": "There.", "start_time": 0.08, "end_time": 0.32,
  "words": [{"word": "there", "punctuated_word": "There.", "start": 0.08, "end": 0.32, "confidence": 0.58}]}]
```

**Participant-keyed:**

```json
[{"participant": {"id": 100, "name": "Ana"},
  "words": [{"text": "Hello", "start_timestamp": {"relative": 1.0}, "end_timestamp": {"relative": 1.4}}]}]
```

Word timings matter. With them, every sentence segment has exact times, and captions follow each word. Without them, times are interpolated (see [PIPELINE.md](PIPELINE.md#transcript-parsing)).

## Setting up bots for the Content Factory

The quality of the package depends on what the bot captured. When you create bots (via REST, MeetStream's MCP, or the dashboard), use these settings:

| Setting | Recommended | Why |
|---|---|---|
| `video_required` (REST) / `record_video` (MCP) | `true` | Video clips need video. The REST API records video by default, but **bots created through MeetStream's MCP are audio-only unless you set `record_video: true`.** |
| `recording_config.video_layout` | `"speaker_view"` | Follows the active speaker. The default `grid_view` mosaic makes poor clips. |
| `recording_config.transcript.provider` | a post-call provider, e.g. `{"meetstream": {"language": "auto"}}` or `{"deepgram": {"model": "nova-3", "diarize": true}}` | A post-call transcript with speaker labels and word timings. `meeting_captions` gives no word timings, and doesn't work on Zoom. |
| `bot_name` / `bot_message` | Make it clear the meeting is being recorded | Supports the consent checkpoint at export |
| `custom_attributes` | e.g. `{"content_factory": "true"}` | Makes webinar bots easy to find with `GET /bots?custom_attr[...]` |

Example REST call:

```bash
curl -X POST https://api.meetstream.ai/api/v1/bots/create_bot \
  -H "Authorization: Token $MEETSTREAM_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "meeting_link": "https://zoom.us/j/123?pwd=...",
    "bot_name": "Webinar Recorder",
    "video_required": true,
    "recording_config": {
      "video_layout": "speaker_view",
      "transcript": {"provider": {"meetstream": {"language": "auto"}}}
    }
  }'
```

> The path and fields come from MeetStream's [Create Bot reference](https://docs.meetstream.ai/api-reference/api-endpoints/bot-endpoints/create-bot). This app doesn't create bots itself, so the call above isn't exercised by its tests.

## Known limits

- **Pagination.** `GET /bots` returns `nextCursor`, but the docs don't name the query parameter that takes it back. The client sends `cursor`, which is **unverified**, and the dashboard only shows the first page. Use the date and status filters to narrow the list.
- **Per-participant streams** (`get_audio_streams`, `get_recording_streams`) aren't used. Speaker tracking for portrait crops would build on them later.
- **In-meeting chat and participant lists** aren't used. Q&A extraction from chat is a planned extension.
- **Webhooks.** There are no webhooks yet; a job is started by a person or agent. Listening for `bot.stopped` / `video.processed` to start jobs automatically is the natural next step.
