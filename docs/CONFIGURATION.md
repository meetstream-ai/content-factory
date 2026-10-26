# Configuration

## Environment variables

Read once at startup (`contentfactory/config.py`). Every one is optional.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (unset) | Enables Claude. `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile in `~/.config/anthropic`, also count. |
| `CF_LLM` | `claude` if credentials are found, else `offline` | Force a generator: `claude` or `offline` |
| `CF_MODEL` | `claude-opus-5-5` | Claude model id |
| `CF_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max`: how hard Claude thinks. Higher is better and slower. |
| `MEETSTREAM_API_KEY` | (unset) | Enables MeetStream import (`ms_…`, from app.meetstream.ai/api-key) |
| `MEETSTREAM_BASE_URL` | `https://api.meetstream.ai/api/v1` | Point at a mock or staging server |
| `CF_DATA_DIR` | `./data` | Database, recordings, renders and exports |
| `CF_JOB_WORKERS` | `2` | Jobs processed at the same time. Each uses CPU for FFmpeg and makes up to 3 concurrent model calls. |
| `CF_API_TOKEN` | (unset) | If set, the API requires `Authorization: Bearer <token>` (or `?access_token=`) |
| `CF_API_URL` | `http://127.0.0.1:8000` | MCP server only: where the API is |
| `CF_NO_APP` | (unset) | Set to `1` to stop `contentfactory.api` from building its module-level app on import (the tests do this) |

The app doesn't read `.env` files itself. Load one into your shell:

```bash
cp .env.example .env     # edit it
set -a; source .env; set +a
```

## Per-job options (`JobConfig`)

Sent as `configuration` when creating a job (JSON body, or a JSON string in the upload form). Stored on the job, so retries use the same settings.

| Field | Type / range | Default | Effect |
|---|---|---|---|
| `audience` | `developers` \| `founders` \| `marketers` \| `general` | `developers` | Described to the model in every writing prompt |
| `tone` | `technical` \| `conversational` \| `educational` \| `thought_leadership` \| `company_blog` | `technical` | Same |
| `clip_count` | 1-12 | 5 | Most clips to keep (fewer if not enough moments pass the score bar) |
| `min_clip_seconds` | number | 15 | Clips are extended to at least this (same speaker, no topic change) |
| `max_clip_seconds` | number | 75 | Clips are trimmed to at most this, on a sentence boundary |
| `clip_formats` | list of `landscape`, `portrait`, `square` | `["landscape", "portrait"]` | One MP4 per format per clip |
| `burn_captions` | bool | `true` | Burn captions into the video. SRT/VTT files are produced either way. |
| `blog_word_count` | 300-3000 | 1000 | Target length of the blog draft |
| `linkedin_post_count` | 1-10 | 4 | Most LinkedIn posts |
| `quote_count` | 1-20 | 8 | Most verbatim quotes (only verified ones are kept) |
| `generate_visual_briefs` | bool | `true` | Skip the design-brief stage if `false` |

Example:

```json
{"audience": "founders", "tone": "thought_leadership", "clip_count": 3,
 "clip_formats": ["portrait"], "burn_captions": true, "blog_word_count": 1500}
```

## Tunable constants in code

These aren't exposed as settings, but are worth knowing about when tuning:

| Where | Constant | Default | What it does |
|---|---|---|---|
| `validate.py` | `CLIP_WEIGHTS` | 25/25/20/15/10/5 | Clip score weights (hook, clarity, education, relevance, engagement, audio) |
| `validate.py` | `MIN_CLIP_SCORE` | 50 | Clips below this are dropped |
| `validate.py` | `LOW_CONFIDENCE` | 0.75 | Transcription confidence that triggers warnings |
| `segmentation.py` | `WINDOW_BLOCKS`, `TRANSITION_BONUS`, `SPEAKER_CHANGE_BONUS`, `MIN_DEPTH` | 3, 0.25, 0.05, 0.05 | Chapter detection sensitivity |
| `segmentation.py` | `target_chapter_count()` | 2×√minutes, 3-9 | Upper bound on chapters |
| `ingest.py` | `MAX_SEGMENT_SECONDS` | 25 | Longest transcript segment |
| `captions.py` | `MAX_WORDS_PER_CUE`, `MAX_CUE_SECONDS` | 7, 3.5 | Caption cue size |
| `media.py` | `FORMATS` | 1920×1080, 1080×1920, 1080×1080 | Output resolutions |
| `meetstream.py` | `with_retry_when_processing` | 10 attempts × 15 s | How long to wait for MeetStream processing |
