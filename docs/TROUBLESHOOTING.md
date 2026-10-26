# Troubleshooting

A failed job shows `error_message` as `"<stage>: <reason>"`, both in the dashboard and in `GET /jobs/{id}`. Fix the cause, then click **Retry from failed stage** (or `POST /jobs/{id}/retry`). Finished stages aren't redone. The server log has the full traceback for unexpected errors.

## Setup

| Symptom | Cause | Fix |
|---|---|---|
| Badge says **Offline mode** | No Anthropic credentials found | `export ANTHROPIC_API_KEY=…` and restart; or force it with `CF_LLM=claude` |
| "Load recordings" is disabled | `MEETSTREAM_API_KEY` not set | Export it and restart |
| `ffmpeg is not installed` | FFmpeg not on PATH | `brew install ffmpeg` / `apt install ffmpeg` |
| `No such filter: 'subtitles'` in a render error | FFmpeg built without libass | Install a full build (Homebrew's `ffmpeg` has it) or set `burn_captions: false` |
| `make_sample.py` fails on `say` | Not macOS | Use your own recording and transcript |
| `ModuleNotFoundError: contentfactory` | Package not installed | `uv pip install -e ".[mcp,dev]"` |
| API returns 401 everywhere | `CF_API_TOKEN` is set | Send `Authorization: Bearer <token>`; the dashboard prompts for it once |

## Ingest

| `error_message` | Cause | Fix |
|---|---|---|
| `…: forbidden (check MEETSTREAM_API_KEY, …)` | Wrong key, or the bot belongs to another MeetStream account | Check the key at app.meetstream.ai |
| `…: not found` | Wrong bot id | Copy it from "Load recordings" |
| `…: no transcript (transcription was not enabled, …)` | The bot had no post-call transcription provider, or never got into the meeting | Run transcription afterwards with MeetStream's `transcribe_audio` tool (or REST equivalent), then retry; for future bots set `recording_config.transcript.provider` |
| `…: still processing` | MeetStream is still processing after ~2 minutes of waiting | Retry in a few minutes |
| `Download failed: HTTP 403` | Presigned link expired (they last ~1 hour) | Retry; a fresh link is fetched |
| `The recording has no audio track…` | Silent file | Upload the right file |
| `No transcript was provided and faster-whisper is not installed…` | Uploaded media without a transcript | Upload the transcript too, or `uv pip install faster-whisper` |
| `Transcript is neither JSON nor SRT/VTT` / `Unrecognised transcript JSON` | Unsupported file | See supported formats in [PIPELINE.md](PIPELINE.md#transcript-parsing) |
| `The transcript contains no speech.` | Empty transcript | Check the file |

## Generation (Claude)

| `error_message` | Cause | Fix |
|---|---|---|
| `Anthropic credentials are missing or invalid` | Bad or expired key | Fix the key, restart, retry |
| `Rate limited by the Anthropic API after retries` | Too many concurrent requests | Retry later; lower `CF_JOB_WORKERS` |
| `Claude declined this step (category: …)` | A safety classifier declined, and the fallback model did too | Usually a false positive on technical security content; retry, or check the transcript for content that shouldn't be repurposed |
| `Output for BlogDraft hit max_tokens` | A very long blog plus thinking | Lower `blog_word_count` or `CF_EFFORT` |
| `Claude's X didn't match the schema` | Rare | Retry |

## Output quality

| Symptom | Why | What to do |
|---|---|---|
| Fewer clips than requested | Candidates under score 50, overlapping, or too short are dropped on purpose | Lower `min_clip_seconds`; check the `clips_filtered` note |
| A clip is shorter than `min_clip_seconds` | It was cut before a topic change, and the next sentence starts a new topic | Expected; edit the times if you want more |
| Clips are audio-only | The bot recorded no video | Set `video_required: true` / `record_video: true` on future bots ([MEETSTREAM.md](MEETSTREAM.md#setting-up-bots-for-the-content-factory)) |
| Clip shows a grid of faces | Bot used `grid_view` | Use `recording_config.video_layout: "speaker_view"` |
| Captions slightly off | No word timings in the source, so times are interpolated | Use a provider with word timings (MeetStream's own transcript has them) |
| Fewer quotes than requested | Proposed quotes that weren't word-for-word were dropped | See the `quotes_dropped` warning for examples |
| Only one or two chapters | Short or single-topic recording, or no clear transitions | Expected; edit chapter titles in review |
| Offline drafts read like the transcript | That's what offline mode does | Add an Anthropic key |

## Review and export

| Message (HTTP 409) | Meaning |
|---|---|
| `This job is still processing; review opens when it finishes.` | Wait for `awaiting_review` |
| `Cannot approve an asset that is CHANGES_REQUESTED…` | Edit or regenerate it first |
| `Say what should change when requesting changes.` | Add feedback |
| `This asset has QA errors. Add a note explaining why it is fine to approve it.` | Add an approval note |
| `A verbatim quote must match the speaker's words…` | Keep the quote exact, or write a takeaway instead |
| `Clip times must be within 0-Ns and at least 3 seconds long.` | Fix the times |
| `Confirm speaker consent and reuse rights…` | `POST /jobs/{id}/consent` (dashboard button) |
| `Nothing is approved yet.` | Approve at least one asset |

## Starting over

Stop the server and delete `data/` (or whatever `CF_DATA_DIR` points at). Everything (database, media, exports) is in there.
