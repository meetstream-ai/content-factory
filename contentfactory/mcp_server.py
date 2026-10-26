"""MCP server exposing the Content Factory as agent tools.

An agent that already uses MeetStream's MCP server (to find meetings) can hand a bot id to
`create_content_package` and come back for the review link. There is deliberately no tool to
approve or publish: review stays with a human in the dashboard.

The tools call the Content Factory HTTP API, so jobs run in the API server's workers and outlive
this process. Configure with CF_API_URL (default http://127.0.0.1:8000) and CF_API_TOKEN.

    claude mcp add content-factory -- contentfactory-mcp
"""

from __future__ import annotations

import os
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer

API = os.environ.get("CF_API_URL", "http://127.0.0.1:8000").rstrip("/")
P = f"{API}/api/v1/content-factory"

server = MCPServer(
    name="meetstream-content-factory",
    instructions=(
        "Turns a recorded MeetStream meeting or webinar into a reviewable content package: chapters, short video "
        "clips with captions, a blog draft, LinkedIn posts, verified quotes and Canva-ready design briefs. Nothing "
        "is ever published; a person approves each asset in the review dashboard. Processing a one-hour recording "
        "takes several minutes, so start a job, then poll get_content_package."
    ),
)


def _client() -> httpx.Client:
    token = os.environ.get("CF_API_TOKEN")
    return httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}"} if token else {})


def _check(r: httpx.Response) -> dict | list:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise RuntimeError(f"Content Factory API {r.status_code}: {detail}")
    return r.json()


def _summary(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job["progress"],
        "current_stage": job["current_stage"],
        "error": job["error_message"],
        "review_required": True,
        "review_url": f"{API}/#job={job['job_id']}",
        "assets": {t: sum(c.values()) for t, c in job.get("asset_counts", {}).items()},
        "warnings": [w["message"] for w in job.get("warnings", [])],
    }


@server.tool()
def list_meetstream_recordings(status: str | None = None, platform: Literal["GMeet", "Zoom", "Teams"] | None = None) -> list[dict]:
    """List MeetStream bots (recorded meetings) on the account, newest first, to pick one to repurpose."""
    with _client() as c:
        data = _check(c.get(f"{API}/api/v1/meetstream/recordings",
                            params={k: v for k, v in {"status": status, "platform": platform}.items() if v}))
    return data["recordings"]


@server.tool()
def create_content_package(
    recording_id: str,
    title: str | None = None,
    audience: Literal["developers", "founders", "marketers", "general"] = "developers",
    tone: Literal["technical", "conversational", "educational", "thought_leadership", "company_blog"] = "technical",
    clip_count: int = 5,
    linkedin_post_count: int = 4,
    blog_word_count: int = 1000,
    include_visual_briefs: bool = True,
) -> dict:
    """Start turning a MeetStream recording (its bot id) into a content package for human review.

    Returns a job id immediately; generation runs in the background. Every asset lands in the review
    queue as PENDING_REVIEW and nothing is published.
    """
    body = {
        "source": {"type": "meetstream_recording", "recording_id": recording_id, "title": title},
        "configuration": {
            "audience": audience, "tone": tone, "clip_count": clip_count, "linkedin_post_count": linkedin_post_count,
            "blog_word_count": blog_word_count, "generate_visual_briefs": include_visual_briefs,
        },
    }
    with _client() as c:
        return _summary(_check(c.post(f"{P}/jobs", json=body)))


@server.tool()
def get_content_package(job_id: str) -> dict:
    """Progress of a content job, and once it is awaiting_review, what was generated and the review link."""
    with _client() as c:
        return _summary(_check(c.get(f"{P}/jobs/{job_id}")))


@server.tool()
def list_package_assets(job_id: str, asset_type: Literal[
        "chapters", "content_brief", "clip", "blog", "linkedin_post", "quote", "takeaway", "visual_brief"] | None = None) -> list[dict]:
    """The generated assets of a job with their review status, for summarising them back to the user."""
    with _client() as c:
        assets = _check(c.get(f"{P}/jobs/{job_id}/assets", params={"asset_type": asset_type} if asset_type else None))
    out = []
    for a in assets:
        content = a["content"]
        preview = content.get("text") or content.get("quote") or content.get("excerpt") or content.get("primary_message") \
            or content.get("transcript_text") or ""
        out.append({"id": a["id"], "type": a["asset_type"], "title": a["title"], "status": a["status"],
                    "version": a["version"], "preview": preview[:400],
                    "qa_issues": [n["message"] for n in a["qa"] if n["level"] in ("error", "warning")]})
    return out


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
