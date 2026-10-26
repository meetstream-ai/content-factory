# Review and export

**Nothing is published automatically.** Every generated asset goes through a human review queue, and the only way out is an export of approved assets into a ZIP. The app has no "publish" action, no social media credentials and no CMS write access.

These rules are enforced by the backend (`review.py`, `pipeline.py`, `export.py`), not just by the buttons in the UI. Calling the API directly gets you the same rules.

## Asset lifecycle

```
GENERATED ──submit──► PENDING_REVIEW ──approve──────────► APPROVED
                          │    ▲                              │
                          │    └────────(submit)── REVISED ◄──┤ edit
                          │                          ▲        │ request_changes
                          ├──request_changes──► CHANGES_REQUESTED ◄┘
                          │                          │
                          │                    edit / regenerate
                          │
                          └──reject──► REJECTED ──reopen──► PENDING_REVIEW
```

| Decision | Allowed from | Goes to | Needs |
|---|---|---|---|
| `submit` (system only) | `GENERATED`, `REVISED` | `PENDING_REVIEW` | |
| `approve` | `PENDING_REVIEW` | `APPROVED` | A note, if the asset has QA **errors** |
| `reject` | `PENDING_REVIEW`, `CHANGES_REQUESTED` | `REJECTED` | |
| `request_changes` | `PENDING_REVIEW`, `APPROVED` | `CHANGES_REQUESTED` | Feedback text |
| `edit` | `PENDING_REVIEW`, `CHANGES_REQUESTED`, `APPROVED` | `REVISED` → `PENDING_REVIEW` | Valid content |
| `regenerate` | `PENDING_REVIEW`, `CHANGES_REQUESTED` | `REVISED` → `PENDING_REVIEW` | Feedback text; a regenerable type |
| `reopen` | `REJECTED` | `PENDING_REVIEW` | |

What these rules guarantee:

- **Nothing skips the queue.** `GENERATED` assets can't be approved; they're submitted when the job finishes.
- **Edits are never self-approving.** Editing an approved asset puts it back in `PENDING_REVIEW`, so whatever is exported is exactly what someone approved.
- **"Changes requested" means changes are made.** It can't be approved until the asset has been edited or regenerated.
- **Review opens when the job finishes.** Actions on a job that's still processing are refused (409).

Every decision is logged in `asset_reviews`: reviewer, decision, feedback, from/to status and version. `GET /assets/{id}` returns this log as `reviews`, and the full version history as `versions`.

## Editing, per asset type

`PATCH /assets/{id}` with `{"content": {...}}` merges the fields you send over the current content. It creates version N+1, with the editor as its author.

| Type | What you can change | What happens |
|---|---|---|
| `clip` | `start`, `end` (seconds), `title` | The clip is **re-rendered** in every format, with fresh captions. Segment ids and transcript text are recomputed. Must be inside the recording and at least 3 s long. |
| `quote` | `quote` | The new text must still be **found in the transcript**; otherwise 409 *"A verbatim quote must match the speaker's words"*. Its timestamp follows the new match. |
| `takeaway` | `text`, `segment_ids` | Free text, since takeaways are labelled AI-written. Citations are re-resolved. |
| `blog` | any `BlogDraft` field | Validated against the schema; paragraph citations are re-checked. |
| `linkedin_post` | `text`, `hashtags`, `angle`, `segment_ids` | Validated; citations re-checked |
| `visual_brief` | any `VisualBrief` field | A quote card's quote is re-verified |
| `content_brief` | any `ContentBrief` field | Takeaway citations re-checked. Existing assets are **not** regenerated automatically, but any later "New variation" request uses the edited brief. |
| `chapters` | the `chapters` list (titles, summaries) | Boundaries must reference real segments; the YouTube text is rebuilt |

Schema errors return 422 with field details.

## "New variation" (regenerate)

`POST /assets/{id}/regenerate` with `{"feedback": "shorter, and lead with the failure story"}` sends the current draft and the feedback back to the generator. Only that asset changes. The previous version stays in the history.

This is available for `blog`, `linkedin_post`, `visual_brief` and `content_brief`. Clips are adjusted by editing their times, and quotes must stay verbatim. In offline mode, regenerate returns the draft unchanged.

## QA notes in review

Each card shows its QA notes (codes are listed in [PIPELINE.md](PIPELINE.md#qa-notes)):

- **error** (red): something is wrong, e.g. a paragraph with no source, a render failure, a quote card that isn't verbatim. Approving anyway requires a note explaining why. That note is exported with the approval.
- **warning** (amber): needs a human look, e.g. low confidence, possibly sensitive source, uncertain speaker.
- **info** (blue): what the system changed, e.g. clip trimmed, opening line dropped.

Job-level warnings, such as sensitive segments, low transcription confidence or dropped quotes, are shown at the top of the job, with timestamp chips to listen to the source.

## Source linking

Every citation in the dashboard is a timestamp chip:
- **Clicking** it seeks the source player to that moment and highlights the line in the transcript.
- **Hovering** shows what was said.

This is how a reviewer checks a blog paragraph or a LinkedIn claim in seconds, instead of searching an hour of video.

## Consent checkpoint

A meeting being recorded doesn't mean everyone in it agreed to have their words turned into public marketing. Before **any** export:

```
POST /jobs/{id}/consent   {"reviewer": "ana", "note": "All speakers signed the webinar release"}
```

This records who confirmed it, their note and when. Export refuses (409) until it's done. The note is written into `manifest.json`.

## Export

```
POST /jobs/{id}/export        → {"id": "exp_…", "download_url": "/api/v1/content-factory/exports/exp_…/download", ...}
```

**Preconditions** (each refused with 409 and a message):
1. The job is `awaiting_review`.
2. Consent is confirmed.
3. At least one asset is `APPROVED`.

**Only approved assets are included.** Rejected, pending and changes-requested assets are listed in the manifest under `not_exported`, but not packaged.

### Package contents

```
content-package/
├── manifest.json            ← always
├── webinar-summary.md       ← if the brief or chapters are approved
├── chapters.json            ← chapters
├── youtube-chapters.txt     ← chapters
├── blog.md                  ← blog (Markdown with front matter)
├── blog.json                ← blog (structured, with citations)
├── linkedin-posts.md        ← approved posts only
├── quotes.json              ← {"verbatim_quotes": [...], "editorial_takeaways": [...]}
├── visual-briefs.json       ← approved briefs
├── clips/clip_01_landscape.mp4, clip_01_portrait.mp4, …
├── captions/clip_01.srt, clip_01.vtt, …
└── source/transcript.json   ← the IR all citations point into
```

`blog.md` contains `[mm:ss]` markers after each paragraph, showing its source. A note at the end says to remove them before publishing. They're there so whoever publishes it can still check claims.

### manifest.json

```json
{
  "generator": "MeetStream Content Factory",
  "exported_at": "2026-10-22T15:10:02+00:00",
  "published": false,
  "job_id": "job_f7dd0cc32a64",
  "webinar": {"id": "web_…", "title": "…", "source_type": "meetstream_recording", "source_ref": "<bot id>", "duration_seconds": 3612.4},
  "consent": "ana: All speakers signed the webinar release (2026-10-22T15:09:40+00:00)",
  "warnings": [ { "level": "warning", "code": "sensitive_content", "message": "…", "segment_ids": ["s0027"] } ],
  "assets": [
    {
      "id": "ast_…", "type": "clip", "title": "The biggest mistake developers make…", "version": 2,
      "status": "APPROVED", "approved_by": "ana", "approved_at": "…", "approval_note": null,
      "source_references": [ {"webinar_id": "web_…", "start_seconds": 41.9, "end_seconds": 63.0, "speaker": "Dev Patel", "segment_ids": ["s0010", "…"]} ],
      "qa": [ {"level": "info", "code": "topic_change", "message": "…"} ],
      "files": { "landscape": {"path": "clips/clip_01_landscape.mp4", "sha256": "…", "bytes": 709161}, "srt": {…} }
    }
  ],
  "not_exported": [ {"id": "ast_…", "type": "linkedin_post", "status": "REJECTED"} ]
}
```

`published` is always `false`. It records that this tool hands off to a person, and never posts anything itself.

Every export is recorded in `asset_exports`. Exporting again creates a new ZIP; the old ones stay on disk. If writing the ZIP fails (e.g. a full disk), it's recorded as a failed export. Retrying the export regenerates nothing.
