"""Package approved assets into a ZIP with a manifest. Exporting never publishes anything anywhere.

Preconditions (each refused with ExportError -> HTTP 409):
  * the job has finished processing;
  * someone confirmed speaker consent and reuse rights for the recording;
  * at least one asset is APPROVED.

Package layout (only files for approved asset types are present):

    content-package/
      manifest.json           every exported asset: version, approver, note, source refs, QA, file checksums;
                              plus job warnings, the consent note, and what was NOT exported
      webinar-summary.md      brief + chapter list
      chapters.json, youtube-chapters.txt
      blog.md, blog.json      blog.md has front matter and [mm:ss] source markers to remove before publishing
      linkedin-posts.md
      quotes.json             {"verbatim_quotes": [...], "editorial_takeaways": [...]} kept apart on purpose
      visual-briefs.json
      clips/clip_01_landscape.mp4, clip_01_portrait.mp4, ...
      captions/clip_01.srt, clip_01.vtt, ...
      source/transcript.json  the IR every source reference points into
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from . import review
from .db import Store, now
from .ir import fmt_clock


class ExportError(RuntimeError):
    """An export precondition isn't met; the message tells the user what to do."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cite(ids: list[str], times: dict[str, float]) -> str:
    """` [04:20, 05:02]`: source markers for exported text, from segment ids."""
    stamps = sorted({fmt_clock(times[i]) for i in ids if i in times})
    return f" [{', '.join(stamps)}]" if stamps else ""


def blog_markdown(content: dict, times: dict[str, float], with_sources: bool = True) -> str:
    """Render a BlogDraft dict as Markdown with YAML front matter (title, meta description, keywords)."""
    def paras(ps):
        return "\n\n".join(p["text"] + (_cite(p["segment_ids"], times) if with_sources else "") for p in ps)

    parts = [f"# {content['title']}", f"*{content['excerpt']}*", paras(content["introduction"])]
    for s in content["sections"]:
        parts += [f"## {s['heading']}", paras(s["paragraphs"])]
    parts += ["## Conclusion", paras(content["conclusion"])]
    if content.get("key_takeaways"):
        parts += ["## Key Takeaways", "\n".join(f"- {k}" for k in content["key_takeaways"])]
    front = (
        "---\n"
        f"title: {json.dumps(content['title'])}\n"
        f"meta_description: {json.dumps(content['meta_description'])}\n"
        f"keywords: {json.dumps(content['keywords'])}\n"
        "---\n\n"
    )
    note = "\n\n---\n*Bracketed timestamps point to the webinar moment each paragraph is based on. Remove before publishing.*\n" if with_sources else "\n"
    return front + "\n\n".join(parts) + note


def build_package(store: Store, job_id: str, job_dir: Path, transcript_path: Path | None) -> Path:
    """Write `<job_dir>/exports/content-package-<timestamp>.zip` and return its path."""
    job = store.get_job(job_id)
    if job is None:
        raise ExportError("Job not found")
    if job["status"] != "awaiting_review":
        raise ExportError("The job hasn't finished processing.")
    if not job["consent_confirmed"]:
        raise ExportError("Confirm speaker consent and reuse rights for this recording before exporting.")
    approved = [a for a in store.list_assets(job_id) if a["status"] == review.APPROVED]
    if not approved:
        raise ExportError("Nothing is approved yet. Approve at least one asset to export.")

    webinar = store.get_webinar(job["webinar_id"])
    times: dict[str, float] = {}
    if transcript_path and transcript_path.exists():
        for s in json.loads(transcript_path.read_text())["segments"]:
            times[s["id"]] = s["start"]

    by_type: dict[str, list[dict]] = {}
    for a in approved:
        by_type.setdefault(a["asset_type"], []).append(a)

    stamp = now().replace(":", "").replace("-", "")[:15]
    out = job_dir / "exports" / f"content-package-{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest_assets = []

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def write_text(name: str, text: str) -> None:
            z.writestr(f"content-package/{name}", text)

        if "content_brief" in by_type or "chapters" in by_type:
            lines = [f"# {webinar['title']}", f"Duration: {fmt_clock(webinar['duration_seconds'] or 0)}", ""]
            for a in by_type.get("content_brief", []):
                c = a["content"]
                lines += [f"**Primary message:** {c['primary_message']}", f"**Audience:** {c['target_audience']}",
                          f"**Call to action:** {c['call_to_action']}", "", "## Key takeaways"]
                lines += [f"- {t['text']}{_cite(t['segment_ids'], times)}" for t in c["key_takeaways"]]
            for a in by_type.get("chapters", []):
                lines += ["", "## Chapters"] + [f"- {fmt_clock(ch['start'])} **{ch['title']}**: {ch['summary']}"
                                                for ch in a["content"]["chapters"]]
            write_text("webinar-summary.md", "\n".join(lines) + "\n")
        for a in by_type.get("chapters", []):
            write_text("chapters.json", json.dumps(a["content"]["chapters"], indent=2))
            write_text("youtube-chapters.txt", a["content"]["youtube_chapters"] + "\n")
        for a in by_type.get("blog", []):
            write_text("blog.md", blog_markdown(a["content"], times))
            write_text("blog.json", json.dumps(a["content"], indent=2))
        if "linkedin_post" in by_type:
            posts = []
            for i, a in enumerate(by_type["linkedin_post"], 1):
                c = a["content"]
                posts.append(f"## Post {i}: {c['angle']}\n\n{c['text']}\n\n{' '.join(c['hashtags'])}\n\n"
                             f"<!-- source{_cite(c['segment_ids'], times)} -->")
            write_text("linkedin-posts.md", "\n\n---\n\n".join(posts) + "\n")
        if "quote" in by_type or "takeaway" in by_type:
            write_text("quotes.json", json.dumps({
                "verbatim_quotes": [a["content"] for a in by_type.get("quote", [])],
                "editorial_takeaways": [a["content"] for a in by_type.get("takeaway", [])],
            }, indent=2))
        if "visual_brief" in by_type:
            write_text("visual-briefs.json", json.dumps([a["content"] for a in by_type["visual_brief"]], indent=2))

        for a in approved:
            files_out = {}
            for kind, rel in a["files"].items():
                src = job_dir / rel
                if not src.exists():
                    continue
                arc = rel if rel.startswith(("clips/", "captions/")) else f"files/{Path(rel).name}"
                z.write(src, f"content-package/{arc}")
                files_out[kind] = {"path": arc, "sha256": _sha256(src), "bytes": src.stat().st_size}
            approvals = [r for r in store.list_reviews(a["id"]) if r["decision"] == "approve"]
            manifest_assets.append({
                "id": a["id"], "type": a["asset_type"], "title": a["title"], "version": a["version"],
                "status": a["status"], "approved_by": approvals[-1]["reviewer_id"] if approvals else None,
                "approved_at": approvals[-1]["reviewed_at"] if approvals else None,
                "approval_note": approvals[-1]["feedback"] if approvals else None,
                "source_references": a["source_references"], "qa": a["qa"], "files": files_out,
            })

        if transcript_path and transcript_path.exists():
            z.write(transcript_path, "content-package/source/transcript.json")
        manifest = {
            "generator": "MeetStream Content Factory",
            "exported_at": now(),
            "published": False,
            "job_id": job_id,
            "webinar": {k: webinar[k] for k in ("id", "title", "source_type", "source_ref", "duration_seconds")},
            "consent": job["consent_note"],
            "warnings": job["warnings"],
            "assets": manifest_assets,
            "not_exported": [{"id": a["id"], "type": a["asset_type"], "status": a["status"]}
                             for a in store.list_assets(job_id) if a["status"] != review.APPROVED],
        }
        write_text("manifest.json", json.dumps(manifest, indent=2))
    return out
