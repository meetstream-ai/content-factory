# Quick start

This takes you from a fresh clone to a reviewed, exported content package in about 10 minutes. Parts 1-4 need no API keys. Parts 5 and 6 switch on real generation with Claude and importing from MeetStream.

## What you need

| Requirement | Check | Install |
|---|---|---|
| Python 3.11+ | `python3 --version` | python.org, or `brew install python@3.12` |
| [uv](https://docs.astral.sh/uv/) (recommended) | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| FFmpeg with libass and libfreetype | `ffmpeg -filters \| grep subtitles` | `brew install ffmpeg` / `apt install ffmpeg` |
| macOS (only for the sample webinar) | `which say` | Elsewhere, skip Part 2 and use your own recording |

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

You should see `25 passed`. The tests render real video with FFmpeg, so they take about 20 seconds.

## Part 2: Make a sample webinar

```bash
.venv/bin/python scripts/make_sample.py samples
```

This takes under a minute. You get:
- `samples/sample_webinar.mp4`: a ~4-minute "webinar" with two speakers, and slides for each section;
- `samples/sample_transcript.json`: its transcript, in MeetStream's own format.

The script uses macOS text-to-speech, so the voices sound robotic. The timings are exact, though, which is what matters here.

## Part 3: Run the app

```bash
.venv/bin/uvicorn contentfactory.api:app --reload
```

Open **http://127.0.0.1:8000**. The badge in the top right says **Offline mode (no LLM)**. That's expected: you haven't added an Anthropic key yet.

> **Offline mode** builds drafts with simple rules, mostly by picking and arranging sentences the speakers actually said. It lets you try the whole workflow, but the writing is not what the product produces with Claude.

## Part 4: Generate, review and export a package

1. In **New content package**, click **Upload**.
2. Choose `samples/sample_webinar.mp4` as the recording and `samples/sample_transcript.json` as the transcript.
3. Set the title to `Building AI Agents That Can Join Meetings` and click **Generate content package**.
4. Watch the stages complete. Offline, the whole job takes about 30 seconds; most of that is FFmpeg rendering 4 clips in two formats.

When the status reads **awaiting review**, you'll see:
- **Warnings** at the top, for example *"1 segment may contain confidential information"*. The sample says "this next part is not public yet" on purpose. Click the timestamp chip in the warning to hear it.
- **Counts** of every asset type.
- The **review queue**: one card per asset.
- On the right, the **source recording** and the full transcript.

Try these:

| Do this | What happens |
|---|---|
| Play a clip | It has burned-in captions. Portrait and landscape versions play side by side. |
| Click any blue timestamp chip | The source player jumps to that moment, and the transcript line is highlighted. |
| **Edit** a clip and change its start/end seconds | Saving re-renders the clip and its captions as version 2, which goes back into the queue. |
| **Edit** a quote and change a word | The save is refused: a verbatim quote must match what the speaker said. |
| **Request changes** on a LinkedIn post | You must say what to change. The post can't be approved until someone edits it. |
| **Approve** a few assets, **Reject** one | The status pills update, and the counts show how many are approved. |
| Click **Export** | It's disabled until you confirm speaker consent. Click **Confirm speaker consent & reuse rights**, then **Export**. |

The ZIP has `manifest.json`, `blog.md` (if you approved the blog), the clips, captions and the transcript. Only approved assets are included. The manifest records who approved each one and when, plus file checksums.

## Part 5: Turn on Claude

Get an API key from https://console.anthropic.com. Then stop the server and run:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/uvicorn contentfactory.api:app --reload
```

The badge now reads **Claude · claude-opus-5-5**. Generate a package from the same sample again: the chapter titles, blog, posts and briefs are now actually written. Expect a few minutes even for a short recording, most of it waiting on the model. The blog, posts and quotes are generated in parallel to keep that down.

To keep keys in a file instead:

```bash
cp .env.example .env    # then edit .env
set -a; source .env; set +a
```

## Part 6: Import from MeetStream

1. Create a MeetStream API key at https://app.meetstream.ai/api-key.
2. Restart the server with it:
   ```bash
   export MEETSTREAM_API_KEY=ms_...
   .venv/bin/uvicorn contentfactory.api:app --reload
   ```
3. In the dashboard, on the **MeetStream recording** tab, click **Load recordings**. Pick a bot (a recorded meeting), give it a title and click **Generate**.

The app fetches the bot's transcript and video, falling back to audio if there's no video, then runs the same pipeline.

**For good video clips, the bot must have recorded video.** MeetStream's REST API records video by default, but bots created through their MCP server are audio-only unless you ask for video. See [MEETSTREAM.md](MEETSTREAM.md#setting-up-bots-for-the-content-factory) for the recommended bot settings.

## Part 7 (optional): Use it from an AI agent

With the server still running:

```bash
claude mcp add content-factory -- "$(pwd)/.venv/bin/contentfactory-mcp"
```

Then, in Claude Code: *"List my MeetStream recordings and create a content package for the latest one, for a developer audience."* See [MCP.md](MCP.md).

## Where things are on disk

Everything lives under `./data` (change it with `CF_DATA_DIR`). Delete the folder to start fresh.

```
data/
  contentfactory.db                 jobs, assets, versions, reviews
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
