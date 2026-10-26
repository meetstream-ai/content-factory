# Documentation

New here? Start with the **[Quick start](QUICKSTART.md)**: about 10 minutes from clone to an exported content package, with no API keys needed.

| Doc | Read it when you want to… |
|---|---|
| [Quick start](QUICKSTART.md) | run the app, generate a package from the sample webinar, review and export it |
| [Architecture](ARCHITECTURE.md) | understand how the pieces fit, the data model, and why it's built this way |
| [Pipeline](PIPELINE.md) | know exactly what each stage does: chapter detection, clip selection, quote checks, rendering |
| [MeetStream integration](MEETSTREAM.md) | import recordings from MeetStream, and set up bots so their recordings work well here |
| [Generation with Claude](GENERATION.md) | understand or change the prompts, model settings, caching and the offline generator |
| [Review and export](REVIEW_AND_EXPORT.md) | learn the review rules, what each edit does, and what's in the exported ZIP |
| [API reference](API.md) | call the HTTP API directly (every endpoint, with curl examples) |
| [MCP server](MCP.md) | let an AI agent create content packages from MeetStream recordings |
| [Configuration](CONFIGURATION.md) | look up environment variables and per-job options |
| [Testing](TESTING.md) | run or extend the test suite |
| [Troubleshooting](TROUBLESHOOTING.md) | fix a failed job or a setup problem |

## The product in one paragraph

You give it a recorded webinar, either a MeetStream bot or an uploaded file. It produces:
- chapters;
- short video clips with captions, in landscape and portrait;
- a blog draft;
- LinkedIn posts;
- quotes, checked word for word against the transcript;
- design briefs for Canva.

Every asset keeps a link to the moment in the recording it came from, so a reviewer can check it in one click. A person approves each asset before it can be exported. Nothing is ever published automatically.

## Code map

```
contentfactory/
  api.py           HTTP API + serves the dashboard              -> API.md
  pipeline.py      job stages, retries, review actions          -> PIPELINE.md, REVIEW_AND_EXPORT.md
  ir.py            transcript segments + source references     -> ARCHITECTURE.md
  ingest.py        transcript parsers (MeetStream, SRT, VTT)    -> PIPELINE.md
  meetstream.py    MeetStream REST client                       -> MEETSTREAM.md
  segmentation.py  chapter boundary detection                   -> PIPELINE.md
  generators.py    Claude + offline generators, prompts         -> GENERATION.md
  drafts.py        structured output schemas, JobConfig         -> GENERATION.md, CONFIGURATION.md
  validate.py      QA: clips, quotes, citations, sensitive text -> PIPELINE.md
  media.py         FFmpeg probing and rendering                 -> PIPELINE.md
  captions.py      SRT / VTT                                    -> PIPELINE.md
  review.py        review state machine                         -> REVIEW_AND_EXPORT.md
  export.py        ZIP package + manifest                       -> REVIEW_AND_EXPORT.md
  db.py            SQLite store                                 -> ARCHITECTURE.md
  mcp_server.py    MCP tools for agents                         -> MCP.md
  config.py        environment settings                         -> CONFIGURATION.md
  static/index.html  review dashboard
scripts/make_sample.py   builds the sample webinar
tests/                   unit + end-to-end tests                -> TESTING.md
```
