# Quick start

This takes you from a fresh clone to a reviewed, downloaded content package in about 10 minutes.

You can start from **a recording file you already have**: drop it in and it's transcribed locally, no keys needed. An **OpenAI** key picks the best moments and writes the content; without it, drafts are rough. A **MeetStream** key lets you paste meeting links and bot ids, and brings transcripts with speaker names.

## What you need

| Requirement | Check | Install |
|---|---|---|
| Python 3.11+ | `python3 --version` | python.org, or `brew install python@3.12` |
| [uv](https://docs.astral.sh/uv/) (recommended) | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| FFmpeg with libass and libfreetype | `ffmpeg -filters \| grep subtitles` | `apt install ffmpeg` (Debian/Ubuntu). Homebrew's `ffmpeg` formula has no libass, so on macOS install a build that has it, or set `burn_captions: false` |
| macOS (only for the sample webinar) | `which say` | Elsewhere, use any recording you have |

## Part 1: Install

```bash
cd meetstream-content-creation
uv venv
uv pip install -e ".[mcp,dev]"
```

Without uv: `python3 -m venv .venv && .venv/bin/pip install -e ".[mcp,dev]"`.

Check that it works:

```bash
.venv/bin/python -m pytest -q
```

You should see `53 passed`. The tests render real video with FFmpeg, so they take about 20 seconds. Without the `subtitles` filter, `test_meetstream_recording_to_exported_package` fails because the clips can't be burned in.

## Part 2: Start the app

```bash
.venv/bin/uvicorn contentfactory.api:app
```

Open **http://127.0.0.1:8000**. The badge in the top right says **AI writing off**. That's expected: you haven't added an OpenAI key yet.

> **Offline mode** builds drafts with simple rules, mostly by picking and arranging sentences the speakers actually said. It lets you try the whole workflow, but the writing is not what the product produces with OpenAI.

## Part 3: Make, review and download a package

You need a recording: any MP4, MOV, WebM, MP3, WAV or M4A of someone talking. No recording to hand? On macOS, `.venv/bin/python scripts/make_sample.py samples` makes a ~4-minute two-speaker webinar at `samples/sample_webinar.mp4`.

1. Optionally open **Options** to set a title, the audience, tone and number of clips.
2. **Drop the file** onto **Drop a recording here** (or click it to choose the file). It uploads with a progress bar.
3. A checklist shows each step as it happens. The first time, the speech-to-text model (~500 MB) downloads, which takes a minute or two. After that, a 4-minute recording takes about 30 seconds: transcription, then FFmpeg cutting the clips in two formats.

You can also **paste a link to the recording file** instead, such as MeetStream's download link for a recording. The box checks it as you paste ("✓ The recording link works … link expires in 42 min"); an expired link is refused straight away.

When it's done you land on the review screen:
- **At the top:** how many items are approved, rejected and still to decide, and the **Download** button.
- **Check before publishing:** for example *"1 segment may contain confidential information"*. The sample says "this next part is not public yet" on purpose. Click the ▶ time to hear it.
- **On the left:** **To review** (everything waiting for a decision), then each content type with how many are approved.
- **In the middle:** one card per item, with **✓ Approve**, **Edit**, **Ask for changes** and **Reject**.
- **On the right:** the original recording and its transcript.

Try these:

| Do this | What happens |
|---|---|
| Play a clip, then switch it to **Vertical** | Captions are burned in; the vertical version is for Reels, Shorts and TikTok. |
| Click any ▶ time | The original recording plays from that moment, and the transcript line is highlighted. |
| **Edit** a clip and change where it starts or ends | Saving re-cuts the clip and its captions, and puts it back in **To review**. "Start at the player's position" sets it from the original recording. |
| **Edit** a quote and change a word | The save is refused: a quote must be word for word what the speaker said. |
| **Ask for changes** on a LinkedIn post | You type what should change. It can't be approved until someone edits it. |
| **Approve** a few items, **Reject** one | Approved items leave **To review** and the counts update. In a single content type, **Approve all** approves the rest of that type. |
| Click **Download** | The first time, it asks you to confirm the speakers agreed and you have the rights to the material. Then it downloads a ZIP. |

The ZIP has `manifest.json`, `blog.md` (if you approved the blog), the clips, captions and the transcript. Only approved assets are included. The manifest records who approved each one and when, plus file checksums.

## Part 4: Turn on OpenAI

Get an API key from https://platform.openai.com/api-keys. In the dashboard, click **Settings** (or the **AI writing off · add key** badge), paste the key and click **Save**. It's checked with OpenAI first: a wrong key, or a key that can't use the chosen model, is refused with the reason. Saved keys go in `data/secrets.json` (readable only by you) and are used from the next package on, with no restart.

The badge now reads **AI: gpt-5-mini**, and cards get a **Rewrite with AI** button. Make a package from the same recording again: OpenAI now picks the clips and actually writes the chapter titles, blog, posts and briefs. Expect a few minutes even for a short recording, most of it waiting on the model. The blog, posts and quotes are generated in parallel to keep that down.

You can also set keys as environment variables instead (`export OPENAI_API_KEY=sk-...` before starting the app, or put them in `.env` and run `set -a; source .env; set +a`). A key saved in Settings takes precedence.

## Part 5: Connect your MeetStream account

1. Create a MeetStream API key at https://app.meetstream.ai/api-key.
2. Paste it into **Settings** → **MeetStream API key** and click **Save**. (Or `export MEETSTREAM_API_KEY=ms_...` before starting the app.)
3. On the home page, either:
   - paste the meeting's own Zoom / Google Meet / Teams link, a MeetStream link to the recording, or a bot id. It shows which recording it found. Or:
   - click **Pick from your recent MeetStream recordings**.

   Then click **Create content**.

The app fetches the bot's transcript and video, falling back to audio if there's no video, then runs the same pipeline.

MeetStream download links now use the bot's transcript too, with speaker names, instead of transcribing locally.

**No MeetStream account, but want to try meeting links?** `scripts/demo_meetstream.py` plays MeetStream locally with the sample webinar:

```bash
.venv/bin/uvicorn --app-dir scripts demo_meetstream:app --port 8765 &
MEETSTREAM_API_KEY=demo MEETSTREAM_BASE_URL=http://127.0.0.1:8765/api/v1 .venv/bin/uvicorn contentfactory.api:app
```

Then paste `https://zoom.us/j/8812345678` (two recordings of that meeting; the newest is used) or `https://meet.google.com/abc-defg-hij` (the bot is still in the meeting, so it's refused with the reason). Stop it before using your real account.

**For good video clips, the bot must have recorded video,** and for a transcript it needs a post-call transcription provider. MeetStream's REST API records video by default, but bots created through their MCP server are audio-only unless you ask for video. See [MEETSTREAM.md](MEETSTREAM.md#setting-up-bots-for-the-content-factory) for the recommended bot settings.

## Part 6 (optional): Use it from an AI agent

With the server still running:

```bash
claude mcp add content-factory -- "$(pwd)/.venv/bin/contentfactory-mcp"    # Claude Code; any MCP client works
```

Then, in the agent: *"List my MeetStream recordings and create a content package for the latest one, for a developer audience."* See [MCP.md](MCP.md).

## Where things are on disk

Everything lives under `./data` (change it with `CF_DATA_DIR`). Delete the folder to start fresh.

```
data/
  contentfactory.db                 jobs, assets, versions, reviews
  uploads/<id>/                     dropped-in files, as received
  webinars/<id>/recording.mp4       the source recording
  webinars/<id>/transcript.json     the transcript every citation points into
  jobs/<id>/clips/*.mp4             rendered clips
  jobs/<id>/captions/*.srt|vtt      captions
  jobs/<id>/exports/*.zip           exported packages
```

## Next

- See the [API reference](API.md) to drive it from scripts.
- See [Review and export](REVIEW_AND_EXPORT.md) for every rule the review queue enforces.
- See [Troubleshooting](TROUBLESHOOTING.md) if a job fails.
