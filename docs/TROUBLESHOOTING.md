# Troubleshooting

A failed job shows `error_message` as `"<stage>: <reason>"`, both in the dashboard and in `GET /jobs/{id}`. Fix the cause, then click **Try again** (or `POST /jobs/{id}/retry`). Finished stages aren't redone. The server log has the full traceback for unexpected errors.

## Setup

| Symptom | Cause | Fix |
|---|---|---|
| Badge says **AI writing off** | No OpenAI key | Click it and add the key in Settings (or `export OPENAI_API_KEY=…` and restart) |
| Settings says *OpenAI didn't accept that key* | Typo, revoked key, or a key from a different provider | Copy it again from platform.openai.com/api-keys |
| Settings says *That key works, but it can't use the model …* | Your OpenAI project has no access to that model | Pick another model in Settings, e.g. `gpt-5-mini` |
| The home page says MeetStream isn't connected | `MEETSTREAM_API_KEY` not set | Meeting links and bot ids need it: export it and restart. Recording links and dropped files work without it. |
| `ffmpeg is not installed` | FFmpeg not on PATH | `brew install ffmpeg` / `apt install ffmpeg` |
| `No such filter: 'subtitles'` in a render error | FFmpeg built without libass | Install a build with libass (Homebrew's `ffmpeg` formula does not include it; Debian/Ubuntu `apt install ffmpeg` does) or set `burn_captions: false` |
| `make_sample.py` fails on `say` | Not macOS | Use your MeetStream account instead of the sample |
| `ModuleNotFoundError: contentfactory` | Package not installed | `uv pip install -e ".[mcp,dev]"` |
| API returns 401 everywhere | `CF_API_TOKEN` is set | Send `Authorization: Bearer <token>`; the dashboard prompts for it once |

## Ingest

| `error_message` | Cause | Fix |
|---|---|---|
| `…: forbidden (check MEETSTREAM_API_KEY, …)` | Wrong key, or the bot belongs to another MeetStream account | Check the key at app.meetstream.ai |
| `MeetStream has no recording with the id …` | Wrong id, or the app's `MEETSTREAM_API_KEY` is for a different account (or `MEETSTREAM_BASE_URL` points at the local demo stand-in) | Check the id; use "Pick from your recent recordings"; check the key and that `MEETSTREAM_BASE_URL` is unset |
| `No MeetStream bot has joined that meeting link on this account…` | No bot recorded that meeting, or it was recorded with a different MeetStream key | Check the API key; if a bot did record it, paste its bot id instead |
| `MeetStream's bot is still in that meeting…` | The meeting hasn't ended | Wait until the bot leaves, then try again |
| `Not a MeetStream link, a Zoom / Google Meet / Teams meeting link, or a bot id.` | Some other kind of link | Paste the meeting's join link or the MeetStream recording link |
| `…: no transcript (transcription was not enabled, …)` | The bot had no post-call transcription provider, or never got into the meeting | Run transcription afterwards with MeetStream's `transcribe_audio` tool (or REST equivalent), then retry; for future bots set `recording_config.transcript.provider`. Or download the recording and drop it in: it's transcribed locally. |
| `…: still processing` | MeetStream is still processing after ~2 minutes of waiting | Retry in a few minutes |
| `Download failed: HTTP 403` | Presigned link expired (they last ~1 hour) | Retry; a fresh link is fetched |
| `That recording link has expired` / `was refused` | MeetStream's download links last about an hour | Copy a fresh link, or download the file and drop it in |
| `No speech was found in the recording.` | Silent recording, or the wrong file | Check the file plays with sound |
| The first local transcription is slow | The Whisper model (~500 MB for `small`) downloads once | Wait; later jobs start straight away. Set `CF_WHISPER_MODEL=base` for a smaller, faster model |
| `The recording has no audio track…` | The bot recorded silence (e.g. muted, or joined an empty meeting) | Check the recording in MeetStream |
| `MeetStream's transcript is not valid JSON` / `Unrecognised transcript JSON` | MeetStream returned a shape we don't parse yet | Check `meetstream_transcript.json` in the webinar folder; see [PIPELINE.md](PIPELINE.md#transcript-parsing) |
| `The transcript contains no speech.` | Empty transcript | Check the file |

## Generation (OpenAI)

| `error_message` | Cause | Fix |
|---|---|---|
| `OpenAI credentials are missing or invalid` | Bad or expired key | Fix the key, restart, retry |
| `Rate limited by the OpenAI API after retries` | Too many concurrent requests | Retry later; lower `CF_JOB_WORKERS` |
| `The model declined this step: …` | The model refused | Usually a false positive on technical security content; retry, or check the transcript for content that shouldn't be repurposed |
| `Output for BlogDraft hit max_output_tokens` | A very long blog plus reasoning | Lower `blog_word_count` or `CF_EFFORT` |
| `The model's X didn't match the schema` | Rare | Retry |

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
| Offline drafts read like the transcript | That's what offline mode does | Add an OpenAI key |

## Review and export

| Message (HTTP 409) | Meaning |
|---|---|
| `This job is still processing; review opens when it finishes.` | Wait for `awaiting_review` |
| `Cannot approve an asset that is CHANGES_REQUESTED…` | Edit or regenerate it first |
| `Say what should change when requesting changes.` | Add feedback |
| `This asset has QA errors. Add a note explaining why it is fine to approve it.` | Add an approval note |
| `A verbatim quote must match the speaker's words…` | Keep the quote exact, or write a takeaway instead |
| `Clip times must be within 0-Ns and at least 3 seconds long.` | Fix the times |
| `Confirm speaker consent and reuse rights…` | `POST /jobs/{id}/consent` (the dashboard asks the first time you click **Download**) |
| `Nothing is approved yet.` | Approve at least one asset |

## Starting over

Stop the server and delete `data/` (or whatever `CF_DATA_DIR` points at). Everything (database, media, exports) is in there.
