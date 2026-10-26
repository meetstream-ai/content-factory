# Architecture

## Overview

```
                 ┌──────────────────────────────┐        ┌───────────────────────────┐
  Browser ──────►│  api.py (FastAPI)            │◄───────│  mcp_server.py            │◄── AI agent
  (dashboard)    │  routes, auth, file serving  │  HTTP  │  4 tools, no approve tool │    (Claude Code…)
                 └──────────────┬───────────────┘        └───────────────────────────┘
                                │ submit / review actions
                 ┌──────────────▼───────────────┐
                 │  pipeline.ContentFactory     │  thread pool (CF_JOB_WORKERS)
                 │  stages, retries, review     │
                 └─┬──────┬──────┬──────┬──────┬┘
                   │      │      │      │      │
      meetstream.py│ ingest.py   │ generators.py      media.py + captions.py
      (REST)       │ (→ IR)      │ (Claude / offline)  (FFmpeg)
                   │      segmentation.py   validate.py (QA)
                   │             │
                 ┌─▼─────────────▼──────────────┐
                 │ db.py (SQLite) + data/ files │
                 └──────────────────────────────┘
```

There is one process: the API server. It owns a thread pool that runs jobs in the background. The MCP server is a separate, thin process that calls the API over HTTP, so a job keeps running after the agent that started it disconnects.

## The central idea: timestamps are data

Every feature depends on one representation, the **transcript IR** (`ir.py`):

```json
{"id": "s0042", "start": 834.1, "end": 841.9, "speaker": "Dev Patel",
 "text": "The biggest mistake developers make…", "words": [...], "confidence": 0.97}
```

- Transcripts in any supported format are split into **sentence-sized segments** with stable ids (`s0001`, `s0002`, …).
- Generators never output timestamps. When something needs to point at the recording, they output **segment ids**.
- Code resolves ids into **SourceRefs**: `{webinar_id, start_seconds, end_seconds, speaker, segment_ids}`. Every asset type stores them in this same shape.

As a result:
- A citation can't point at a moment that doesn't exist. Unknown ids are dropped and flagged.
- Clips start and end on real sentence boundaries.
- Any citation can be turned into a "jump to this moment" button, which is how review works.
- The export ships `transcript.json`, so the references stay meaningful outside the app.

## Deterministic code vs. the model

| Work | Done by | Why |
|---|---|---|
| Fetching, converting, cutting media | Code (httpx, FFmpeg) | Must be exact and repeatable |
| Splitting the transcript into sentences | Code | Citations depend on stable ids |
| Where chapters start | Code (TextTiling-style) | Reproducible, always on a sentence |
| Naming and summarising chapters | Model | Needs language understanding |
| Which moments make good clips, sub-scores | Model | Needs judgment |
| Clip boundaries, trimming, total score | Code | The model's timing can't be trusted |
| Brief, blog, posts, takeaways, design briefs | Model | It's writing |
| Is this quote verbatim? Are citations valid? | Code | An LLM's opinion is not evidence |
| Is the clip playable and the right length? | Code (ffprobe) | Same |
| Approve / reject | A person | The product rule: nothing is published automatically |

## Request flow: creating a package

1. `POST /jobs` (MeetStream) or `POST /uploads` creates a **job** row, status `queued`, and returns 202 at once.
2. A worker thread runs `ContentFactory.run_job`:
   - `ingest` creates a **webinar** row: the recording plus `transcript.json` on disk.
   - `chapters` → `content_brief` → `clip_selection` run in order.
   - `blog`, `social` and `quotes` run concurrently.
   - `visual_briefs` → `render_clips` → `quality_validation` run in order.
   - Each stage writes **assets** (status `GENERATED`) and records itself in the job's `completed` list.
3. `quality_validation` moves every asset to `PENDING_REVIEW`, and the job becomes `awaiting_review`.
4. The dashboard polls `GET /jobs/{id}` every 2 s, then loads `GET /jobs/{id}/assets`.

If a stage raises, the job becomes `failed` with `error_message = "<stage>: <reason>"`. `POST /jobs/{id}/retry` resumes at the failed stage. See [PIPELINE.md](PIPELINE.md#failures-and-retries).

## Data model

```
webinars 1──* content_jobs 1──* content_assets 1──* asset_versions
                           │                   1──* asset_reviews
                           1──* asset_exports
```

| Table | Holds | Key columns |
|---|---|---|
| `webinars` | One recording + transcript | `source_type` (`meetstream_recording`/`upload`), `source_ref` (bot id or filename), `recording_path`, `has_video`, `duration_seconds`, `transcript_path` |
| `content_jobs` | One run of the pipeline | `status` (`queued`/`processing`/`awaiting_review`/`failed`), `configuration` (JobConfig), `source`, `progress`, `current_stage`, `completed[]`, `warnings[]`, `error_message`, `generator`, `consent_confirmed`, `consent_note` |
| `content_assets` | The current version of each asset | `asset_type`, `position`, `title`, `content` (JSON), `files` (kind → relative path), `source_references[]`, `qa[]`, `status`, `version` |
| `asset_versions` | Every version, including the current one | `version`, `content`, `files`, `source_references`, `author` (generator name or editor) |
| `asset_reviews` | Every decision | `reviewer_id`, `decision`, `feedback`, `from_status`, `to_status`, `version` |
| `asset_exports` | Every ZIP built | `destination` (`zip`), `path`, `asset_ids[]`, `export_status`, `error_message` |

**Asset types** and their `content` shapes:

| `asset_type` | One per | `content` | `files` |
|---|---|---|---|
| `chapters` | job | `{chapters: [{index, title, summary, keywords, start, end, first_segment_id, last_segment_id}], youtube_chapters}` | none |
| `content_brief` | job | `ContentBrief` (drafts.py) | none |
| `clip` | clip | `{title, start_segment_id, end_segment_id, start, end, duration, score, score_breakdown, reason, platforms, speaker, transcript_text}` | `srt`, `vtt`, `landscape`/`portrait`/`square` (or `audio`) |
| `blog` | job | `BlogDraft`: intro/sections/conclusion paragraphs, each with `segment_ids` | none |
| `linkedin_post` | post | `SocialPost` | none |
| `quote` | quote | `{kind: "verbatim", quote, speaker, segment_ids, start, end, timestamp, confidence, why}` | none |
| `takeaway` | takeaway | `{kind: "editorial", label, text, segment_ids}` | none |
| `visual_brief` | brief | `VisualBrief` | none |

Each type is a separate asset, rather than one big package document, so each item can be approved or rejected on its own. Rejecting one LinkedIn post leaves the others untouched.

**QA notes** (`qa[]`) look like `{level: error|warning|info, code, message}`. The full list of codes is in [PIPELINE.md](PIPELINE.md#qa-notes).

## Files on disk

```
$CF_DATA_DIR (default ./data)
  contentfactory.db
  uploads/up_<id>/recording.mp4, transcript.json     as received
  webinars/web_<id>/
      recording.mp4 | recording.wav                  source media
      transcript.json                                the IR
      meetstream_bot.json, meetstream_transcript.json   raw MeetStream responses (MeetStream source only)
      audio_16k.wav                                  only if we transcribed locally
  jobs/job_<id>/
      clips/clip_01_landscape.mp4, clip_01_portrait.mp4, …
      captions/clip_01.srt, clip_01.vtt, …
      exports/content-package-<timestamp>.zip
```

The database stores paths, not bytes. Media is served by `FileResponse`, which supports HTTP range requests, so video seeking works.

## Concurrency

- **Jobs:** a `ThreadPoolExecutor` with `CF_JOB_WORKERS` threads (default 2). Extra jobs queue.
- **Within a job:** blog, social and quotes run on a three-thread pool. Clips render one at a time.
- **Database:** one SQLite connection in WAL mode, shared across threads and serialised by an `RLock`. `Store.transaction()` wraps read-modify-write sequences, such as appending a job warning or saving a new asset version.
- **Review vs. processing:** review actions are refused until the job is `awaiting_review`, so a reviewer never races a running stage.

## Design decisions and trade-offs

| Decision | Alternative considered | Why this way |
|---|---|---|
| A fixed workflow with model calls at specific stages | A swarm of autonomous agents | Easier to test, debug and resume; costs are predictable |
| REST for MeetStream ingestion | Claude + MeetStream MCP tools | Downloads and timestamps must be exact; no model tokens spent moving bytes |
| SQLite + local files + thread pool | PostgreSQL + S3 + Celery/Temporal | MVP scope. `Store` and the stage functions are the seams for swapping these. |
| One asset per item | One asset per type | Per-item approval, as the spec asks |
| Chapter boundaries from text statistics | Ask the model for chapters | Reproducible; always on a sentence; cheaper |
| Keep the whole frame, centred on a blur, for portrait | Centre crop | Webinars are slides and screen shares, which a crop would cut off |
| Offline generator | Require an API key | Lets tests and demos run the full workflow |

## Scaling path

The code is single-node on purpose. To scale it:

1. **Store.** Swap SQLite for PostgreSQL. The SQL is portable; `Store` is the only module that touches it.
2. **Jobs.** Move `run_job` onto Celery or Temporal. Stages are already resumable units, with completion recorded in the database.
3. **Files.** Move media to S3-compatible storage. `files` already holds relative paths, so a storage abstraction can resolve them.
4. **Renders.** Run clip rendering as separate tasks. Each clip is independent.
5. **Hand-off from MeetStream.** Use their `bot.stopped` / `video.processed` webhooks instead of polling, and start jobs automatically.
