# MeetStream integration

[MeetStream](https://docs.meetstream.ai) sends bots into Zoom, Google Meet and Microsoft Teams calls to record and transcribe them. The Content Factory takes a finished bot's recording and transcript and turns them into a content package.

In MeetStream terms, **a bot is one recorded meeting**, so in this app a *recording id* is a MeetStream *bot id*.

## Pointing it at a recording

Paste any of these into the dashboard's link box, the API's `source.link`, or the MCP tool's `recording` argument:

| You paste | Example | What happens |
|---|---|---|
| A bot id | `5b6c7d8e-1f2a-4b3c-9d4e-5f6a7b8c9d0e` | Checked with `GET /bots/{id}/detail`, then used |
| A MeetStream link | `https://app.meetstream.ai/…/5b6c7d8e-1f2a-…` (dashboard, API or any `*.meetstream.ai` URL) | The bot id is taken from the URL, then checked the same way |
| The meeting's own link | `https://zoom.us/j/8812345678?pwd=…`, `https://meet.google.com/abc-defg-hij`, a Teams join link | The app finds the bot MeetStream sent to that meeting and uses its recording |
| MeetStream's download link for the recording | `https://<bucket>.s3.amazonaws.com/<bot id>/meeting_recording.mp4?X-Amz-…` | Downloaded directly; **works without a MeetStream key**. With a key, the bot's transcript is used; without one, it's transcribed locally. These links expire after about an hour, and an expired one is refused with that reason. |

The first three need `MEETSTREAM_API_KEY`. You can also drop a recording file into the dashboard, which needs no MeetStream at all.

**How meeting links are matched.** The app reads your bots (`GET /bots`, following pages) and compares each bot's `meeting_link` with yours. MeetStream documents List Bots entries only loosely. So for any bot listed without a meeting link, the app reads it from `GET /bots/{id}/detail` (`MeetingLink`), newest bots first, for up to 50 bots. The comparison ignores the differences between copies of the same invite:

| Platform | Two links count as the same meeting if… |
|---|---|
| Zoom | they have the same meeting number (`/j/<number>`), whatever the subdomain (`us02web.zoom.us`) or `?pwd=` |
| Google Meet | they have the same meeting code, ignoring case and query parameters |
| Teams | they have the same join path, after URL-decoding |

**When several bots match** (a recurring meeting link), the **newest** one that has finished is used. The dashboard says *"newest of N recordings of this meeting"*, and the API returns the others as `other_matches`. To pick an older one, paste its bot id instead.

**When the link or id can't be used,** the job is refused immediately with the reason, and nothing is queued:

| Situation | Message |
|---|---|
| No bot on this account joined that meeting | *No MeetStream bot has joined that meeting link on this account…* |
| No bot with that id on this account (bot id or MeetStream link) | *MeetStream has no recording with the id … on this account. Check the id, and that MEETSTREAM_API_KEY is a key for the MeetStream account that recorded it.* |
| The bot belongs to another MeetStream account | *Recording … belongs to a different MeetStream account than MEETSTREAM_API_KEY.* |
| The bot is still in the meeting | *MeetStream's bot is still in that meeting (status: InMeeting). Try again after the meeting ends.* |
| The bot was refused entry | *MeetStream's bot for that meeting didn't record it (status: NotAllowed).* |
| A MeetStream URL with no bot id in it | *That MeetStream link doesn't contain a bot id…* |
| Anything else, e.g. a YouTube link | *Not a MeetStream link, a Zoom / Google Meet / Teams meeting link, or a bot id.* |

Bot statuses that mean "still going" are `Scheduled`, `Joining`, `InWaitingRoom`, `InMeeting` and `Recording`. `NotAllowed`, `Denied` and failure statuses mean "no recording". Anything else, such as `Stopped`, `Done` or a status we don't know, is treated as finished. If its media isn't ready yet, ingest waits on MeetStream's 202 responses as usual.

To check what a link resolves to without starting a job, call `GET /api/v1/meetstream/resolve?link=…`. The dashboard does this when you paste a link.

> This app finds recordings MeetStream has already made. It doesn't send bots into meetings. To record a new meeting, create a bot with MeetStream (see [below](#setting-up-bots-for-the-content-factory)), then paste the meeting link here once it's over.

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
| "Pick from your recent recordings" in the dashboard | `GET /bots?status=&platform=&from=&to=` | `list_bots` | Picking a bot |
| A meeting link is pasted | `GET /bots` (all pages, up to 20) | `list_bots` | Finding the bot that recorded that meeting |
| Ingest step 1 | `GET /bots/{bot_id}/detail` | `get_bot_detail` | Platform and date for the default title |
| Ingest step 2 | `GET /transcript/{transcript_id}/get_transcript` | `get_transcript` | The speaker-labelled transcript (`transcript_id` from the bot detail) |
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
[{"speaker": "Alex Rivera", "transcript": "There.", "start_time": 0.08, "end_time": 0.32,
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

- **Pagination.** `GET /bots` returns `nextCursor`, but the docs don't name the query parameter that takes it back. The client sends `cursor`, which is **unverified**. Meeting-link lookup follows pages, and stops if the API returns a page it has already seen. So if `cursor` turns out to be wrong, it still searches the first page rather than looping. The dashboard's "Pick from your recent recordings" list shows only the first page. A bot id or MeetStream link doesn't depend on pagination.
- **Per-participant streams** (`get_audio_streams`, `get_recording_streams`) aren't used. Speaker tracking for portrait crops would build on them later.
- **In-meeting chat and participant lists** aren't used. Q&A extraction from chat is a planned extension.
- **Webhooks.** There are no webhooks yet; a job is started by a person or agent. Listening for `bot.stopped` / `video.processed` to start jobs automatically is the natural next step.
