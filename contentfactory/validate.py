"""Deterministic quality checks. An LLM's opinion is not evidence that a quote is real or a clip plays.

Each check returns QA notes shaped `{"level": "error"|"warning"|"info", "code": ..., "message": ...}`,
which travel with the asset into the review queue and the export manifest:

  error    something is wrong (uncited paragraph, quote card not verbatim, render failed).
           Approving such an asset requires a written justification.
  warning  needs a human look (low transcription confidence, possibly sensitive source, speaker uncertain).
  info     what the system changed and why (clip trimmed, opening line dropped, word count).

Codes in use: unknown_segment, unsupported, boundary_adjusted, trimmed, extended, too_short,
topic_change, low_confidence, multi_speaker, speaker_uncertain, sensitive_content, quote_not_verbatim,
render_failed, no_recording, no_video, quotes_dropped, clips_filtered, word_count.
"""

from __future__ import annotations

import re

from .drafts import ClipCandidate, JobConfig
from .ir import Segment, Transcript, normalize_text
from .segmentation import TRANSITION

# Clip score = sum(sub_score[0-10] * weight) / 10, giving 0-100. Weights are the product spec's
# starting heuristics, not a validated engagement model; tune them against approval rates.
CLIP_WEIGHTS = {
    "hook_strength": 25,
    "standalone_clarity": 25,
    "educational_value": 20,
    "audience_relevance": 15,
    "engagement_potential": 10,
    "audio_quality": 5,
}
MIN_CLIP_SCORE = 50  # below this a clip is dropped: fewer good suggestions beat padding with weak ones
LOW_CONFIDENCE = 0.75  # mean word confidence under which captions/quotes likely contain errors

# Openers that only make sense with earlier context; a clip shouldn't start on one.

_BACKREF_START = re.compile(
    r"^(so,? )?(as i (said|mentioned)|like i said|as we (saw|discussed)|going back to|and (also|then)|also,|"
    r"which|that's why|in the previous)", re.I)


def note(level: str, code: str, message: str, **extra) -> dict:
    """Build a QA note. `extra` carries structured detail (e.g. `segment_ids`) for the UI."""
    return {"level": level, "code": code, "message": message, **extra}


# ---------------------------------------------------------------- references


def resolve_refs(transcript: Transcript, segment_ids: list[str], what: str) -> tuple[list[dict], list[dict]]:
    """SourceRefs for the ids, merged into contiguous runs, plus notes for ids that don't exist.

    Ids within two segments of each other are merged into one reference, so a paragraph citing
    s0040, s0041 and s0043 gets one "jump here" link rather than three.
    """
    notes = []
    unknown = [s for s in segment_ids if not transcript.has(s)]
    if unknown:
        notes.append(note("warning", "unknown_segment", f"{what} cited segments that don't exist: {', '.join(unknown[:5])}"))
    known = sorted({s for s in segment_ids if transcript.has(s)}, key=transcript.index)
    if not known:
        notes.append(note("error", "unsupported", f"{what} has no valid source reference."))
        return [], notes
    runs, run = [], [known[0]]
    for sid in known[1:]:
        if transcript.index(sid) - transcript.index(run[-1]) <= 2:
            run.append(sid)
        else:
            runs.append(run)
            run = [sid]
    runs.append(run)
    return [transcript.ref(r).model_dump() for r in runs], notes


# ---------------------------------------------------------------- quotes


def locate_quote(transcript: Transcript, quote: str, hint_segment_id: str | None) -> list[Segment] | None:
    """The segments a quote's exact wording appears in, or None if it was paraphrased or invented.

    Matching is on normalised text (case, punctuation and curly quotes ignored; words must be
    identical). A quote may span up to four consecutive segments. The search starts at the
    segment the model cited and widens outwards, so a slightly wrong citation is corrected
    rather than rejected.
    """
    target = normalize_text(quote)
    if len(target) < 8:
        return None
    segs = transcript.segments
    order = list(range(len(segs)))
    if hint_segment_id and transcript.has(hint_segment_id):
        h = transcript.index(hint_segment_id)
        order = sorted(order, key=lambda i: abs(i - h))  # look near the cited segment first
    for i in order:
        joined, span = "", []
        for j in range(i, min(i + 4, len(segs))):
            joined = (joined + " " + normalize_text(segs[j].text)).strip()
            span.append(segs[j])
            if target in joined:
                # Trim leading segments the quote doesn't actually touch.
                while len(span) > 1 and target in " ".join(normalize_text(s.text) for s in span[1:]):
                    span.pop(0)
                return span
            if len(joined) > len(target) + 400:
                break
    return None


# ---------------------------------------------------------------- clips


def _audio_quality(segs: list[Segment]) -> float:
    """0-10 from transcription confidence (our proxy for audio quality); 8 when the provider gives none."""
    confs = [s.confidence for s in segs if s.confidence is not None]
    return 8.0 if not confs else max(0.0, min(10.0, 10 * sum(confs) / len(confs)))


def resolve_clip(c: ClipCandidate, transcript: Transcript, cfg: JobConfig) -> tuple[dict | None, list[dict]]:
    """Turn a candidate into exact times on sentence boundaries, or reject it with a reason.

    Steps, each recorded as an info note when it changes something:
      1. Unknown segment ids -> reject.
      2. Opening sentence refers back ("as I mentioned...") -> drop it, if the rest is long enough.
      3. A later sentence announces a new topic ("Moving on to...") -> end before it.
      4. Longer than max_clip_seconds -> drop trailing sentences until it fits.
      5. Shorter than min_clip_seconds -> add the same speaker's next sentences (never a topic change).
      6. Pad 0.2 s before / 0.35 s after so first and last words aren't clipped, without
         running into neighbouring speech or past the recording.
      7. Compute the weighted score in code from the model's sub-scores.
    Returns `(clip_content, notes)`, or `(None, notes)` if rejected.
    """
    notes: list[dict] = []
    if not (transcript.has(c.start_segment_id) and transcript.has(c.end_segment_id)):
        return None, [note("error", "unknown_segment", f"Clip '{c.title}' cites segments that don't exist.")]
    segs = transcript.span(c.start_segment_id, c.end_segment_id)
    all_segs = transcript.segments

    # Don't open on a back-reference if the clip can stand without its first sentence.
    if _BACKREF_START.search(segs[0].text) and len(segs) > 1 and segs[-1].end - segs[1].start >= cfg.min_clip_seconds:
        notes.append(note("info", "boundary_adjusted", f"Dropped opening line that refers back to earlier content: \"{segs[0].text[:60]}\""))
        segs = segs[1:]

    # A clip is one idea: stop before the speaker announces the next topic.
    for k in range(1, len(segs)):
        if TRANSITION.search(segs[k].text):
            notes.append(note("info", "topic_change", f"Ended before the speaker moves on: \"{segs[k].text[:60]}\""))
            segs = segs[:k]
            break

    while len(segs) > 1 and segs[-1].end - segs[0].start > cfg.max_clip_seconds:
        segs = segs[:-1]
        if not any(n["code"] == "trimmed" for n in notes):
            notes.append(note("info", "trimmed", f"Trimmed to end on a sentence boundary within {cfg.max_clip_seconds:.0f}s."))

    last = transcript.index(segs[-1].id)
    while segs[-1].end - segs[0].start < cfg.min_clip_seconds and last + 1 < len(all_segs) \
            and all_segs[last + 1].speaker == segs[-1].speaker and not TRANSITION.search(all_segs[last + 1].text):
        last += 1
        segs = segs + [all_segs[last]]
        if not any(n["code"] == "extended" for n in notes):
            notes.append(note("info", "extended", "Extended to finish the speaker's thought."))
    if segs[-1].end - segs[0].start < min(cfg.min_clip_seconds, 8):
        return None, [note("error", "too_short", f"Clip '{c.title}' is too short to stand alone.")]

    # Small pads so words aren't clipped, kept inside the recording and clear of neighbouring speech.
    first_idx = transcript.index(segs[0].id)
    prev_end = all_segs[first_idx - 1].end if first_idx > 0 else 0.0
    next_start = all_segs[last + 1].start if last + 1 < len(all_segs) else transcript.duration
    start = min(segs[0].start, max(0.0, prev_end, segs[0].start - 0.2))
    end = max(segs[-1].end, min(transcript.duration, next_start, segs[-1].end + 0.35))

    sub = {
        "hook_strength": c.hook_strength, "standalone_clarity": c.standalone_clarity,
        "educational_value": c.educational_value, "audience_relevance": c.audience_relevance,
        "engagement_potential": c.engagement_potential, "audio_quality": _audio_quality(segs),
    }
    sub = {k: max(0.0, min(10.0, float(v))) for k, v in sub.items()}
    score = round(sum(sub[k] * w for k, w in CLIP_WEIGHTS.items()) / 10)
    if _audio_quality(segs) < 10 * LOW_CONFIDENCE:
        notes.append(note("warning", "low_confidence", "Transcription confidence is low here; check the captions."))
    if len({s.speaker for s in segs}) > 1:
        notes.append(note("info", "multi_speaker", "More than one speaker in this clip."))

    return {
        "title": c.title,
        "start_segment_id": segs[0].id,
        "end_segment_id": segs[-1].id,
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(end - start, 3),
        "score": score,
        "score_breakdown": sub,
        "reason": c.reason,
        "platforms": c.platforms,
        "speaker": segs[0].speaker if len({s.speaker for s in segs}) == 1 else None,
        "transcript_text": " ".join(s.text for s in segs),
    }, notes


def dedupe_clips(clips: list[tuple[dict, list[dict]]], limit: int) -> list[tuple[dict, list[dict]]]:
    """Best first; drop clips overlapping a better one by more than 30%, and anything under the score bar."""
    kept: list[tuple[dict, list[dict]]] = []
    for clip, notes in sorted(clips, key=lambda x: x[0]["score"], reverse=True):
        if clip["score"] < MIN_CLIP_SCORE:
            continue
        overlap = any(
            min(clip["end"], k["end"]) - max(clip["start"], k["start"]) > 0.3 * min(clip["duration"], k["duration"])
            for k, _ in kept
        )
        if not overlap:
            kept.append((clip, notes))
        if len(kept) >= limit:
            break
    return sorted(kept, key=lambda x: x[0]["start"])


# ---------------------------------------------------------------- whole-recording checks

_SENSITIVE = re.compile(
    r"\b(confidential|internal only|under nda|off the record|don'?t share|not public yet|unannounced|"
    r"password|api key|social security|salary|revenue (was|is) \$?\d)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\b(\+?\d{1,2}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")


def recording_warnings(transcript: Transcript) -> list[dict]:
    """Job-level warnings about the recording itself: low confidence, possibly sensitive lines.

    The sensitive-content check is a simple pattern match (confidential, NDA, "not public
    yet", emails, phone numbers...). It is meant to prompt a human check, not to be complete.
    """
    out = []
    confs = [s.confidence for s in transcript.segments if s.confidence is not None]
    if confs and sum(confs) / len(confs) < LOW_CONFIDENCE:
        out.append(note("warning", "low_confidence", f"Average transcription confidence is {sum(confs) / len(confs):.0%}; "
                        "expect caption errors."))
    flagged = [s for s in transcript.segments if _SENSITIVE.search(s.text) or _EMAIL.search(s.text) or _PHONE.search(s.text)]
    if flagged:
        out.append(note("warning", "sensitive_content",
                        f"{len(flagged)} segment(s) may contain confidential or personal information. "
                        "Make sure no asset reuses them without sign-off.",
                        segment_ids=[s.id for s in flagged[:20]]))
    return out


def sensitive_overlap(refs: list[dict], warnings: list[dict]) -> list[dict]:
    """A warning for an asset whose sources include a segment flagged as possibly sensitive."""
    flagged = {sid for w in warnings if w["code"] == "sensitive_content" for sid in w.get("segment_ids", [])}
    hit = sorted({sid for r in refs for sid in r.get("segment_ids", [])} & flagged)
    if hit:
        return [note("warning", "sensitive_content", f"Draws on segments flagged as possibly sensitive: {', '.join(hit)}")]
    return []

