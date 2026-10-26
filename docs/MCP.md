# MCP server: the Content Factory as an agent tool

`contentfactory-mcp` is an [MCP](https://modelcontextprotocol.io) server. It lets AI agents (Claude Code, Claude Desktop, any MCP client) start content packages and report on them.

It pairs naturally with **MeetStream's own MCP server**: the agent uses MeetStream's tools to find the right meeting, then this server's tools to turn it into content.

> *"Take yesterday's product webinar, find the best moments, create LinkedIn posts, and get everything ready for my review."*

## How it fits

```
Agent ──MCP──► MeetStream MCP (hosted)  list_bots, get_bot_detail, …        → finds the bot id
      └─MCP──► contentfactory-mcp (stdio) ──HTTP──► Content Factory API    → starts and tracks the job
                                                       └─ dashboard: a person reviews and exports
```

`contentfactory-mcp` is a thin client of the HTTP API. Jobs run in the API server, so they keep going after the agent's session ends. **The API server must be running.**

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `list_meetstream_recordings` | `status?`, `platform?` (`GMeet`/`Zoom`/`Teams`) | `[{bot_id, name, status, platform, created_at, …}]` |
| `create_content_package` | `recording_id` (bot id), `title?`, `audience?`, `tone?`, `clip_count?` (5), `linkedin_post_count?` (4), `blog_word_count?` (1000), `include_visual_briefs?` (true) | Job summary (below) |
| `get_content_package` | `job_id` | Job summary |
| `list_package_assets` | `job_id`, `asset_type?` | `[{id, type, title, status, version, preview, qa_issues}]` |

The job summary looks like this:

```json
{"job_id": "job_123", "status": "processing", "progress": 65, "current_stage": "render_clips", "error": null,
 "review_required": true, "review_url": "http://127.0.0.1:8000/#job=job_123",
 "assets": {"clip": 5, "blog": 1, "linkedin_post": 4}, "warnings": []}
```

**There is deliberately no tool to approve, edit, export or publish.** An agent can prepare the work and point a person at the review link, but approval stays with a human. This is the product's core rule, and keeping these tools out of the MCP surface means a prompt-injected or over-eager agent can't get around it.

## Setup

### 1. Run the API

```bash
.venv/bin/uvicorn contentfactory.api:app
```

### 2. Add the server to your client

**Claude Code:**

```bash
claude mcp add content-factory \
  -e CF_API_URL=http://127.0.0.1:8000 \
  -- /absolute/path/to/meetstream-content-creation/.venv/bin/contentfactory-mcp
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "content-factory": {
      "command": "/absolute/path/to/meetstream-content-creation/.venv/bin/contentfactory-mcp",
      "env": {"CF_API_URL": "http://127.0.0.1:8000"}
    }
  }
}
```

If the API requires a token, add `CF_API_TOKEN` to the env as well.

### 3. Add MeetStream's MCP server (optional, recommended)

From [MeetStream's docs](https://docs.meetstream.ai/build-with-ai/meetstream-mcp-server):

```bash
claude mcp add --transport http meetstream https://mcp.meetstream.ai/mcp
# then /mcp → meetstream → Authenticate
```

MeetStream's MCP uses `Authorization: Bearer <key>`, while its REST API (used by this app) uses `Token <key>`. The same key works for both.

## Example session

> **You:** Find my Zoom recordings from this week and make a content package from the product webinar, for founders.
>
> **Agent:** calls `list_meetstream_recordings(platform="Zoom")` (or MeetStream's own `list_bots`), finds `bot_42` *"Product webinar"*, then calls `create_content_package(recording_id="bot_42", title="Product Webinar: Q4 Launch", audience="founders")`.
>
> **Agent:** "Started job_7f3… I'll check on it." It calls `get_content_package` until `status` is `awaiting_review`.
>
> **Agent:** calls `list_package_assets(job_7f3…)`. "Done: 5 clips, a 1,000-word blog draft, 4 LinkedIn posts and 8 verified quotes. One clip has a warning: it draws on a segment flagged as possibly confidential (12:04). Review and approve here: http://127.0.0.1:8000/#job=job_7f3…"

## Testing the server without a client

```bash
.venv/bin/python - <<'EOF'
import asyncio
from contentfactory.mcp_server import server
async def main():
    for t in await server.list_tools():
        print(t.name, list(t.input_schema["properties"]))
asyncio.run(main())
EOF
```

This uses the MCP Python SDK v2 (`mcp>=2.3`), where the server class is `MCPServer`; it was `FastMCP` in v1.
