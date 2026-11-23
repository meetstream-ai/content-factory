# API reference

Base URL: `http://127.0.0.1:8000` by default. All bodies are JSON. FastAPI also serves interactive docs at **`/docs`** (Swagger UI) and the schema at `/openapi.json`.

```bash
B=http://127.0.0.1:8000
P=$B/api/v1/content-factory
```

## Authentication

Off by default. When `CF_API_TOKEN` is set, every endpoint except `/api/v1/health` and `/` needs either of:

```
Authorization: Bearer <token>
?access_token=<token>          # for <video>/<audio> src and download links, which can't send headers
```

A missing or wrong token gives `401 {"detail": "Missing or invalid API token"}`.

There are no user accounts. The `reviewer` field in review requests is recorded as given; the dashboard sends `"reviewer"`. Put the API behind your SSO proxy if you need verified identities.

## Errors

Errors look like `{"detail": "<message>"}`, or a list of field errors for 422.

| Status | When |
|---|---|
| 400 | Bad request, e.g. MeetStream isn't configured, or an action isn't possible |
| 401 | Token required |
| 404 | Job, asset, file or export not found |
| 409 | A rule refused the action: review state, consent missing, nothing approved, job still processing, retrying a job that hasn't failed |
| 422 | Body failed validation (configuration, edited content) |
| 502 | MeetStream or the model failed during a synchronous call |

---

## Health

### `GET /api/v1/health`

```json
{"ok": true, "llm_mode": "openai", "model": "gpt-5-mini", "meetstream_configured": true, "auth_required": false,
 "local_transcription": "small"}
```

`llm_mode` is `"openai"` or `"offline"`. `model` is `null` offline. `local_transcription` is the Whisper model used for recordings without a MeetStream transcript.

## Settings

### `GET /api/v1/settings`

The keys the app is using, without revealing them:

```json
{"openai_api_key": {"set": true, "source": "saved", "hint": "…1234"}, "openai_model": "gpt-5-mini",
 "meetstream_api_key": {"set": false, "source": null, "hint": null}, "llm_mode": "openai"}
```

`source` is `saved` (set from the dashboard) or `environment`.

### `POST /api/v1/settings`

```bash
curl -X POST $B/api/v1/settings -H 'Content-Type: application/json' \
  -d '{"openai_api_key": "sk-...", "openai_model": "gpt-5-mini", "meetstream_api_key": "ms_..."}'
```

Fields left out are unchanged; `""` removes a saved value. A new key is checked first (OpenAI: `models.retrieve` of the model; MeetStream: List Bots), and a key the service refuses returns **422** with the reason and saves nothing. On success the values are saved to `data/secrets.json` (mode 600) and used from the next job on. Returns the same shape as `GET`.

---

## MeetStream recordings

### `GET /api/v1/meetstream/resolve?link=…`

Which recording a pasted link refers to: a bot id, a MeetStream URL, a Zoom / Meet / Teams meeting link, or a direct link to a recording file. See [MEETSTREAM.md](MEETSTREAM.md#pointing-it-at-a-recording).

```bash
curl -G "$B/api/v1/meetstream/resolve" --data-urlencode "link=https://zoom.us/j/8812345678?pwd=abc"
```

```json
{"bot_id": "bot_42", "resolved_from": "meeting_link",
 "bot": {"bot_id": "bot_42", "status": "Stopped", "platform": "Zoom", "meeting_link": "https://us02web.zoom.us/j/8812345678?pwd=abc", "created_at": "2026-10-21T15:00:00Z", "…": "…"},
 "other_matches": [{"bot_id": "bot_7", "status": "Stopped", "created_at": "2026-09-01T15:00:00Z", "…": "…"}]}
```

`resolved_from` is `bot_id`, `meetstream_url`, `meeting_link` or `recording_url`. `bot` is MeetStream's summary of the bot (none for recording links); `other_matches` lists older recordings of the same meeting. `transcript` is `"meetstream"` or `"local"` (transcribed here).

For a recording link, the response also has `url`, `size` (bytes) and `expires_at` (for presigned links), and needs no MeetStream key:

```json
{"resolved_from": "recording_url", "bot_id": "2e3f4a5b-…", "bot": null, "other_matches": [], "transcript": "local",
 "url": "https://<bucket>.s3.amazonaws.com/2e3f4a5b-…/meeting_recording.mp4?…",
 "size": 1265338, "expires_at": "2026-11-03T15:21:31+00:00"}
```

Returns 422 with the reason if the link leads to no usable recording (including an expired download link, or a meeting link while `MEETSTREAM_API_KEY` isn't set), and 502 if MeetStream fails.

### `GET /api/v1/meetstream/recordings`

Query (all optional): `status`, `platform` (`GMeet` | `Zoom` | `Teams`), `date_from`, `date_to` (`YYYY-MM-DD`).

```bash
curl "$B/api/v1/meetstream/recordings?platform=Zoom"
```

```json
{
  "recordings": [
    {"bot_id": "5b6c7d8e-…", "name": "Webinar Recorder", "status": "Stopped", "platform": "Zoom",
     "meeting_link": "https://zoom.us/j/…", "created_at": "2026-10-21T15:00:00Z", "duration": 3612, "custom_attributes": {}}
  ],
  "has_next_page": false,
  "next_cursor": null
}
```

Returns 400 if `MEETSTREAM_API_KEY` isn't set, and 502 if MeetStream returns an error.

---

## Jobs

### `POST /api/v1/content-factory/jobs`: package a recording from a link

```bash
# By meeting link (or a MeetStream link, or a bot id): resolved to the meeting's recording
curl -X POST $P/jobs -H 'Content-Type: application/json' -d '{
  "source": {"type": "meetstream_recording", "link": "https://zoom.us/j/8812345678?pwd=abc", "title": "Building Production AI Agents"},
  "configuration": {"audience": "developers", "tone": "technical", "clip_count": 5, "blog_word_count": 1000}
}'

# By bot id
curl -X POST $P/jobs -H 'Content-Type: application/json' -d '{
  "source": {"type": "meetstream_recording", "recording_id": "5b6c7d8e-1f2a-4b3c-9d4e-5f6a7b8c9d0e"}
}'

# By a direct link to the recording file, e.g. MeetStream's download link (no MeetStream key needed)
curl -X POST $P/jobs -H 'Content-Type: application/json' -d '{
  "source": {"type": "meetstream_recording", "link": "https://<bucket>.s3.amazonaws.com/<bot id>/meeting_recording.mp4?X-Amz-…"}
}'
```

- Give **either** `source.link` **or** `source.recording_id`:
  - `link` takes a bot id, any MeetStream URL containing one, the meeting's own Zoom / Meet / Teams link, or a direct link to a recording file. It's checked before the job is created. A link that leads nowhere returns **422** with the reason, and no job is created.
  - A **recording link** is checked by fetching its first byte (an expired presigned link is refused), and needs no MeetStream key. The job's `source` becomes `{"type": "recording_url", "url", "bot_id"}`: if the link is a MeetStream download link, `bot_id` is taken from it and MeetStream's transcript is used when `MEETSTREAM_API_KEY` is set; otherwise the recording is transcribed locally.
  - Every other kind needs `MEETSTREAM_API_KEY` (**422** without it), and the job's `source` records `bot_id` and `resolved_from`.
  - `recording_id` (alias `bot_id`) is the MeetStream bot id.
  - Either way, the recording is checked with MeetStream first: an unknown id, another account's bot, or a bot still in its meeting returns **422** with the reason.
- `title` is optional. The default is "<Platform> recording <date>", or the file name for recording links.
- `configuration` is optional. Every field is described in [CONFIGURATION.md](CONFIGURATION.md#per-job-options-jobconfig).

The response is **202** with the job (see [Job object](#job-object)). Processing happens in the background.

### `POST /api/v1/content-factory/uploads`: package a recording file

Multipart form. The recording is transcribed locally (no keys needed).

| Field | Required | Notes |
|---|---|---|
| `recording` | yes | `.mp4 .mov .webm .mkv .m4a .mp3 .wav` (anything else is **415**) |
| `title` | no | Default: from the file name |
| `configuration` | no | JobConfig as a JSON **string** |

```bash
curl -X POST $P/uploads -F recording=@meeting_recording.mp4 -F 'configuration={"clip_count": 3}'
```

The response is **202** with the job, whose `source` is `{"type": "upload", "filename", …}`. A file larger than `CF_MAX_UPLOAD_MB` (default 4096) is **413**.

### `GET /api/v1/content-factory/jobs`

The 50 most recent jobs:

```json
[{"job_id": "job_f7dd0cc32a64", "status": "awaiting_review", "progress": 100,
  "title": "Building AI Agents That Can Join Meetings", "created_at": "2026-10-22T14:54:54+00:00",
  "assets": 29, "approved": 5, "to_review": 22}]
```

`assets`, `approved` and `to_review` count the job's assets, for showing review progress in a list.

### `GET /api/v1/content-factory/jobs/{job_id}`

Poll this until `status` is `awaiting_review` or `failed`.

#### Job object

```json
{
  "job_id": "job_f7dd0cc32a64",
  "status": "processing",
  "progress": 65,
  "current_stage": "render_clips",
  "completed": ["ingest", "chapters", "content_brief", "clip_selection", "blog", "social", "quotes", "visual_briefs"],
  "pending": ["render_clips", "quality_validation"],
  "error_message": null,
  "warnings": [
    {"level": "warning", "code": "sensitive_content",
     "message": "1 segment(s) may contain confidential or personal information. …", "segment_ids": ["s0027"]}
  ],
  "generator": "openai",
  "configuration": {"audience": "developers", "tone": "technical", "clip_count": 5, "…": "…"},
  "source": {"type": "meetstream_recording", "link": "https://zoom.us/j/8812345678", "bot_id": "3f2b9c1e-…", "resolved_from": "meeting_link", "title": "…"},
  "consent_confirmed": false,
  "consent_note": null,
  "created_at": "2026-10-22T14:54:54+00:00",
  "completed_at": null,
  "webinar": {"id": "web_899db6a91ea4", "title": "…", "source_type": "meetstream_recording", "source_ref": "3f2b9c1e-…",
              "duration_seconds": 227.05, "has_video": 1},
  "asset_counts": {"clip": {"PENDING_REVIEW": 4}, "blog": {"PENDING_REVIEW": 1}, "…": {}},
  "exports": []
}
```

| `status` | Meaning |
|---|---|
| `queued` | Waiting for a worker |
| `processing` | Running; see `current_stage` |
| `awaiting_review` | Done; assets are in the review queue |
| `failed` | `error_message` = `"<stage>: <reason>"`; retry is possible |

`webinar` is `null` until ingest finishes.

### `POST /api/v1/content-factory/jobs/{job_id}/retry`

Re-queues a **failed** job from the stage that failed. Returns 202 with the job, or 409 if the job hasn't failed.

### `GET /api/v1/content-factory/jobs/{job_id}/transcript`

The transcript IR, available once ingest is done. Use it to turn segment ids into times and text:

```json
{"webinar_id": "web_…", "duration": 227.05, "segments": [
  {"id": "s0010", "start": 42.084, "end": 49.49, "speaker": "Dev Patel",
   "text": "The biggest mistake developers make when building AI agents is …", "words": [ … ], "confidence": 0.97}
]}
```

### `GET /api/v1/content-factory/jobs/{job_id}/recording`

The source media file. Supports HTTP `Range`, so players can seek.

### `POST /api/v1/content-factory/jobs/{job_id}/consent`

```bash
curl -X POST $P/jobs/$J/consent -H 'Content-Type: application/json' \
  -d '{"reviewer": "ana", "note": "All speakers signed the webinar release"}'
```

Returns the job, with `consent_confirmed: true`. This is required before export.

### `POST /api/v1/content-factory/jobs/{job_id}/export`

Packages the **approved** assets into a ZIP.

```json
{"id": "exp_3f2c…", "job_id": "job_…", "destination": "zip", "path": "/…/exports/content-package-20261022T151002.zip",
 "asset_ids": ["ast_…", "ast_…"], "export_status": "completed", "error_message": null,
 "exported_at": "2026-10-22T15:10:02+00:00",
 "download_url": "/api/v1/content-factory/exports/exp_3f2c…/download"}
```

409 if the job is still processing, consent isn't confirmed, or nothing is approved. The package format is in [REVIEW_AND_EXPORT.md](REVIEW_AND_EXPORT.md#package-contents).

### `GET /api/v1/content-factory/exports/{export_id}/download`

The ZIP file.

---

## Assets

### `GET /api/v1/content-factory/jobs/{job_id}/assets`

Optional query: `asset_type` (`chapters`, `content_brief`, `clip`, `blog`, `linkedin_post`, `quote`, `takeaway`, `visual_brief`).

#### Asset object

```json
{
  "id": "ast_6af4a6e281eb",
  "job_id": "job_f7dd0cc32a64",
  "asset_type": "clip",
  "position": 0,
  "title": "The biggest mistake developers make when building AI agents...",
  "content": {
    "title": "…", "start_segment_id": "s0010", "end_segment_id": "s0013",
    "start": 41.884, "end": 62.9, "duration": 21.0, "score": 78,
    "score_breakdown": {"hook_strength": 9, "standalone_clarity": 8, "educational_value": 5,
                        "audience_relevance": 10, "engagement_potential": 6, "audio_quality": 9.7},
    "reason": "…", "platforms": ["linkedin", "youtube_shorts"], "speaker": "Dev Patel", "transcript_text": "…"
  },
  "files": {"srt": "captions/clip_01.srt", "vtt": "captions/clip_01.vtt",
            "landscape": "clips/clip_01_landscape.mp4", "portrait": "clips/clip_01_portrait.mp4"},
  "file_urls": {"srt": "/api/v1/content-factory/assets/ast_6af4a6e281eb/files/srt", "…": "…"},
  "source_references": [{"webinar_id": "web_…", "start_seconds": 41.884, "end_seconds": 62.9,
                         "speaker": "Dev Patel", "segment_ids": ["s0010", "s0013"]}],
  "qa": [{"level": "info", "code": "topic_change", "message": "Ended before the speaker moves on: \"Moving on to how…\""}],
  "status": "PENDING_REVIEW",
  "version": 1,
  "allowed_decisions": ["approve", "reject", "request_changes", "edit"],
  "created_at": "…", "updated_at": "…"
}
```

`content` depends on `asset_type`; the shapes are listed in [ARCHITECTURE.md](ARCHITECTURE.md#data-model). `allowed_decisions` lists exactly the actions the backend will accept right now.

### `GET /api/v1/content-factory/assets/{asset_id}`

The asset, plus:
- `versions`: every version's title, content, files, refs, author and time;
- `reviews`: the decision log, with reviewer, decision, feedback, from/to status, version and time.

### `GET /api/v1/content-factory/assets/{asset_id}/files/{kind}`

A file from `files`: `srt`, `vtt`, `landscape`, `portrait`, `square` or `audio`.

### `GET /api/v1/content-factory/clips/{asset_id}/preview?format=portrait`

The rendered clip in that format (default `landscape`). Audio-only sources return the audio excerpt instead.

### `PATCH /api/v1/content-factory/assets/{asset_id}`: edit

```bash
# Move a clip's boundaries (re-renders video and captions)
curl -X PATCH $P/assets/$CLIP -H 'Content-Type: application/json' \
  -d '{"reviewer": "ana", "title": "Tool access is not agency", "content": {"start": 42.0, "end": 63.5}}'

# Rewrite a LinkedIn post
curl -X PATCH $P/assets/$POST -H 'Content-Type: application/json' \
  -d '{"reviewer": "ana", "content": {"text": "Most teams think…"}}'
```

`content` is **merged** over the current content. The response is the asset with `versions` and `reviews`, now at version N+1 and `PENDING_REVIEW`. Per-type rules are in [REVIEW_AND_EXPORT.md](REVIEW_AND_EXPORT.md#editing-per-asset-type).

### Review actions

All take `{"reviewer": "<name>", "feedback": "<text or null>"}` and return the updated asset with `versions` and `reviews`.

| Endpoint | Notes |
|---|---|
| `POST …/assets/{id}/approve` | `feedback` is required if the asset has QA errors (it becomes the approval note) |
| `POST …/assets/{id}/reject` | |
| `POST …/assets/{id}/request-changes` | `feedback` is required |
| `POST …/assets/{id}/reopen` | Rejected → pending |
| `POST …/assets/{id}/regenerate` | `feedback` is required; blog, linkedin_post, visual_brief and content_brief only; synchronous (one model call) |

```bash
curl -X POST $P/assets/$ID/approve -H 'Content-Type: application/json' -d '{"reviewer": "ana"}'
curl -X POST $P/assets/$ID/request-changes -H 'Content-Type: application/json' \
  -d '{"reviewer": "ana", "feedback": "Lead with the failure story; cut the hashtags to two."}'
```

A refused transition returns **409**, e.g. `"Cannot approve an asset that is CHANGES_REQUESTED (allowed from: PENDING_REVIEW)."`

---

## End-to-end script

Against real MeetStream, use your own meeting link. Against the local stand-in (`scripts/demo_meetstream.py`), the link below works as is.

```bash
set -e
B=http://127.0.0.1:8000; P=$B/api/v1/content-factory
J=$(curl -s -X POST $P/jobs -H 'Content-Type: application/json' \
      -d '{"source": {"type": "meetstream_recording", "link": "https://zoom.us/j/8812345678", "title": "Sample"}}' \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')
until curl -s $P/jobs/$J | grep -qE '"status":"(awaiting_review|failed)"'; do sleep 2; done

# Approve every clip and the blog
for ID in $(curl -s "$P/jobs/$J/assets" | python3 -c '
import sys, json
for a in json.load(sys.stdin):
    if a["asset_type"] in ("clip", "blog"): print(a["id"])'); do
  curl -s -X POST $P/assets/$ID/approve -H 'Content-Type: application/json' -d '{"reviewer":"script"}' >/dev/null
done

curl -s -X POST $P/jobs/$J/consent -H 'Content-Type: application/json' -d '{"reviewer":"script","note":"demo"}' >/dev/null
URL=$(curl -s -X POST $P/jobs/$J/export | python3 -c 'import sys,json; print(json.load(sys.stdin)["download_url"])')
curl -s -o package.zip $B$URL && unzip -l package.zip
```
