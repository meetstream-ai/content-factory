# MeetStream Content Factory

Turn one recorded webinar into a content package: chapters, short video clips with captions, a blog draft, LinkedIn posts, verified quotes and Canva-ready design briefs. Each asset is tied to the moment in the recording it came from, and each one goes through human review before export. **Nothing is ever published.**

```
MeetStream bot / upload ─► transcript IR ─► chapters ─► content brief ─┬─► clips ─► FFmpeg render ─┐
                            (segment ids)   (TextTiling   (shared story)  ├─► blog                    ├─► QA ─► review queue ─► ZIP export
                                             + Claude)                    ├─► LinkedIn posts           │
                                                                          └─► quotes ─► visual briefs ─┘
```

## Quick start

Needs Python 3.11+, FFmpeg (`brew install ffmpeg`) and [uv](https://docs.astral.sh/uv/). No API keys are needed to try it.

```bash
uv venv && uv pip install -e ".[mcp,dev]"
.venv/bin/python scripts/make_sample.py samples          # a ~4 min sample webinar (macOS)
.venv/bin/uvicorn contentfactory.api:app --reload
```

1. Open http://127.0.0.1:8000.
2. Choose **Upload**, then add `samples/sample_webinar.mp4` and `samples/sample_transcript.json`.
3. Click **Generate content package**.
4. Review the assets: click any timestamp chip to jump the source player there.
5. Approve a few, confirm consent, then **Export**.

Without an API key the app runs in **offline mode**: it builds drafts by rearranging the speakers' own sentences, which is enough to try the workflow. Then add real generation and MeetStream import:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # Claude writes the content
export MEETSTREAM_API_KEY=ms_...         # "Load recordings" pulls your MeetStream bots
.venv/bin/uvicorn contentfactory.api:app --reload
```

The full walkthrough is in **[docs/QUICKSTART.md](docs/QUICKSTART.md)**.

## Documentation

| | |
|---|---|
| [Quick start](docs/QUICKSTART.md) | Clone to exported package, step by step |
| [Architecture](docs/ARCHITECTURE.md) | Components, data model, design decisions |
| [Pipeline](docs/PIPELINE.md) | Every stage: chapter detection, clip selection, quote verification, rendering, retries |
| [MeetStream integration](docs/MEETSTREAM.md) | Which endpoints are used, transcript formats, recommended bot settings |
| [Generation with Claude](docs/GENERATION.md) | Prompts, caching, structured outputs, the offline generator |
| [Review and export](docs/REVIEW_AND_EXPORT.md) | Review rules, edits, consent, package and manifest format |
| [API reference](docs/API.md) | Every endpoint with curl examples |
| [MCP server](docs/MCP.md) | Use it from Claude Code / Claude Desktop alongside MeetStream's MCP |
| [Configuration](docs/CONFIGURATION.md) | Environment variables and per-job options |
| [Testing](docs/TESTING.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) | |

## Key ideas

- **Timestamps are data.** Transcripts become sentence segments with ids (`s0042`). Claude cites ids, and code turns them into times, so every source reference points at a real moment.
- **Code checks what the model claims:**
  - chapter boundaries come from topic-shift detection;
  - clips snap to sentences and stop before the speaker changes topic;
  - quotes must match the transcript word for word;
  - rendered files are re-probed.
- **One shared brief** keeps the blog, posts, quotes and visuals telling one story.
- **The backend enforces review.**
  - Editing an approved asset sends it back for review.
  - Approving an asset with QA errors needs a note.
  - Export needs a consent confirmation, and includes only approved assets.

## Tests

```bash
.venv/bin/python -m pytest      # ~20 s, no network or keys needed
```

## Not built yet

These are Phase 2+ in the product spec:
- YouTube import;
- Google Drive, Notion, Ghost and Slack integrations;
- filling Canva templates automatically;
- speaker-tracking crops and animated captions;
- multiple reviewer roles;
- brand profiles, and "Ask the Webinar".

Jobs run in-process (a thread pool over SQLite). The scaling path is in [ARCHITECTURE.md](docs/ARCHITECTURE.md#scaling-path).

## About MeetStream

[MeetStream](https://meetstream.ai) is meeting bot infrastructure. A bot joins Google Meet, Zoom, or Microsoft Teams, records the call, and returns a transcript and structured meeting data through an API.

- Website: https://meetstream.ai
- Docs: https://docs.meetstream.ai
- GitHub: https://github.com/meetstream-ai
