# API reference

Base URL: `http://127.0.0.1:8000` by default. All bodies are JSON unless noted. FastAPI also serves interactive docs at **`/docs`** (Swagger UI) and the schema at `/openapi.json`.

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
| 415 | Unsupported upload file type |
| 422 | Body failed validation (configuration, edited content) |
| 502 | MeetStream or the model failed during a synchronous call |

---

## Health

### `GET /api/v1/health`

```json
{"ok": true, "llm_mode": "claude", "model": "claude-opus-5-5", "meetstream_configured": true, "auth_required": false}
```

`llm_mode` is `"claude"` or `"offline"`. `model` is `null` offline.

---

## MeetStream recordings

### `GET /api/v1/meetstream/recordings`

Query (all optional): `status`, `platform` (`GMeet` | `Zoom` | `Teams`), `date_from`, `date_to` (`YYYY-MM-DD`).

```bash
curl "$B/api/v1/meetstream/recordings?platform=Zoom"
```

```json
{
  "recordings": [
    {"bot_id": "0c0f8353-…", "name": "Webinar Recorder", "status": "Stopped", "platform": "Zoom",
     "meeting_link": "https://zoom.us/j/…", "created_at": "2026-10-21T15:00:00Z", "duration": 3612, "custom_attributes": {}}
  ],
  "has_next_page": false,
  "next_cursor": null
}
```

Returns 400 if `MEETSTREAM_API_KEY` isn't set, and 502 if MeetStream returns an error.

---

## Jobs

### `POST /api/v1/content-factory/jobs`: package a MeetStream recording

```bash
curl -X POST $P/jobs -H 'Content-Type: application/json' -d '{
  "source": {"type": "meetstream_recording", "recording_id": "0c0f8353-…", "title": "Building Production AI Agents"},
  "configuration": {"audience": "developers", "tone": "technical", "clip_count": 5, "blog_word_count": 1000}
}'
```

- `source.recording_id` is the MeetStream bot id; `bot_id` is accepted as well.
- `title` is optional. The default is "<Platform> recording <date>".
- `configuration` is optional. Every field is described in [CONFIGURATION.md](CONFIGURATION.md#per-job-options-jobconfig).

The response is **202** with the job (see [Job object](#job-object)). Processing happens in the background.

### `POST /api/v1/content-factory/uploads`: package an uploaded recording

Multipart form:

| Field | Required | Notes |
|---|---|---|
| `recording` | one of the two | `.mp4 .mov .webm .mkv .mp3 .wav .m4a` |
| `transcript` | one of the two | MeetStream JSON, `.srt` or `.vtt`. Without it, local transcription needs faster-whisper. |
| `title` | no | Default "Untitled webinar" |
| `configuration` | no | JobConfig as a JSON **string** |

```bash
curl -X POST $P/uploads \
  -F recording=@samples/sample_webinar.mp4 \
  -F transcript=@samples/sample_transcript.json \
  -F "title=Building AI Agents That Can Join Meetings" \
  -F 'configuration={"clip_count": 3, "clip_formats": ["portrait"]}'
```

The response is **202** with the job. A transcript-only upload (no recording) produces every text asset, plus captions without video.

### `GET /api/v1/content-factory/jobs`

The 50 most recent jobs:

```json
[{"job_id": "job_f7dd0cc32a64", "status": "awaiting_review", "progress": 100,
  "title": "Building AI Agents That Can Join Meetings", "created_at": "2026-10-22T14:54:54+00:00"}]
```

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
  "generator": "claude",
  "configuration": {"audience": "developers", "tone": "technical", "clip_count": 5, "…": "…"},
  "source": {"type": "upload", "title": "…", "filename": "sample_webinar.mp4", "…": "…"},
  "consent_confirmed": false,
  "consent_note": null,
  "created_at": "2026-10-22T14:54:54+00:00",
  "completed_at": null,
  "webinar": {"id": "web_899db6a91ea4", "title": "…", "source_type": "upload", "source_ref": "sample_webinar.mp4",
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

```bash
set -e
B=http://127.0.0.1:8000; P=$B/api/v1/content-factory
J=$(curl -s -X POST $P/uploads -F recording=@samples/sample_webinar.mp4 -F transcript=@samples/sample_transcript.json \
      -F title=Sample | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')
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
