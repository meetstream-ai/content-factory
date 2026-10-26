from pathlib import Path

import pytest

from contentfactory import review, segmentation, validate
from contentfactory.drafts import ClipCandidate, JobConfig
from contentfactory.ingest import parse_transcript

from .conftest import SAMPLES


def cand(start, end, **scores):
    base = dict(hook_strength=8, standalone_clarity=8, educational_value=7, audience_relevance=7, engagement_potential=6)
    return ClipCandidate(start_segment_id=start, end_segment_id=end, title="t", reason="r", platforms=["linkedin"],
                         **{**base, **scores})


def test_verbatim_quote_is_found_and_paraphrase_is_not(transcript):
    span = validate.locate_quote(transcript, "A reliable agent is one that knows what to do when its tools fail", "s0001")
    assert [s.id for s in span] == ["s0007"]  # the cited id was wrong; the real location is used
    assert validate.locate_quote(transcript, "Reliable agents handle tool failure gracefully", "s0007") is None


def test_quote_spanning_two_segments(transcript):
    span = validate.locate_quote(transcript, "tool access is the easy part, and knowing when to act is the hard part. An agent that interrupts", None)
    assert [s.id for s in span] == ["s0004", "s0005"]


def test_clip_resolves_to_sentence_boundaries_with_padding(transcript):
    clip, notes = validate.resolve_clip(cand("s0003", "s0005"), transcript, JobConfig(min_clip_seconds=5))
    s3, s5 = transcript.get("s0003"), transcript.get("s0005")
    assert clip["start"] == pytest.approx(s3.start - 0.2, abs=0.01)
    assert clip["end"] == pytest.approx(s5.end + 0.35, abs=0.01)
    assert clip["speaker"] == "Dev Patel"
    # 25/25/20/15/10/5 weights over 0-10 sub-scores -> 0-100
    assert clip["score"] == round((8 * 25 + 8 * 25 + 7 * 20 + 7 * 15 + 6 * 10 + 9.5 * 5) / 10)


def test_clip_trimmed_to_max_length_on_a_sentence(transcript):
    # s0006-s0009 is ~17.5 s with no topic change inside it.
    clip, notes = validate.resolve_clip(cand("s0006", "s0009"), transcript, JobConfig(min_clip_seconds=5, max_clip_seconds=15))
    assert clip["end_segment_id"] == "s0008" and clip["duration"] <= 15
    assert any(n["code"] == "trimmed" for n in notes)


def test_clip_ends_before_the_speaker_changes_topic(transcript):
    # s0006 opens with "Moving on to production..." (a new topic), so the clip stops at s0005.
    clip, notes = validate.resolve_clip(cand("s0003", "s0007"), transcript, JobConfig(min_clip_seconds=5))
    assert clip["end_segment_id"] == "s0005"
    assert any(n["code"] == "topic_change" for n in notes)


def test_clip_with_unknown_segment_is_rejected(transcript):
    clip, notes = validate.resolve_clip(cand("s0003", "s9999"), transcript, JobConfig())
    assert clip is None and notes[0]["code"] == "unknown_segment"


def test_low_scoring_and_overlapping_clips_are_dropped(transcript):
    cfg = JobConfig(min_clip_seconds=5)
    good = validate.resolve_clip(cand("s0003", "s0004"), transcript, cfg)
    overlap = validate.resolve_clip(cand("s0004", "s0005", hook_strength=6), transcript, cfg)
    weak = validate.resolve_clip(cand("s0007", "s0008", hook_strength=1, standalone_clarity=1, educational_value=1,
                                      audience_relevance=1, engagement_potential=1), transcript, cfg)
    kept = validate.dedupe_clips([good, overlap, weak], limit=5)
    assert [c["start_segment_id"] for c, _ in kept] == ["s0003"]


def test_resolve_refs_flags_unknown_and_unsupported(transcript):
    refs, notes = validate.resolve_refs(transcript, ["s0002", "s0003", "s0009", "s0404"], "Paragraph")
    assert [r["segment_ids"] for r in refs] == [["s0002", "s0003"], ["s0009"]]
    assert notes[0]["code"] == "unknown_segment"
    refs, notes = validate.resolve_refs(transcript, [], "Paragraph")
    assert refs == [] and notes[0]["code"] == "unsupported"


def test_sensitive_content_is_flagged(transcript):
    warnings = validate.recording_warnings(transcript)
    flagged = next(w for w in warnings if w["code"] == "sensitive_content")
    assert flagged["segment_ids"] == ["s0009"]
    hit = validate.sensitive_overlap([{"segment_ids": ["s0008", "s0009"]}], warnings)
    assert hit and "s0009" in hit[0]["message"]


@pytest.mark.skipif(not (SAMPLES / "sample_transcript.json").exists(), reason="run scripts/make_sample.py first")
def test_chapter_boundaries_land_on_spoken_transitions():
    t = parse_transcript("w", (SAMPLES / "sample_transcript.json").read_bytes(), "s.json")
    starts = segmentation.detect_boundaries(t)
    texts = [t.segments[i].text for i in starts[1:]]
    assert len(starts) >= 3
    assert all(segmentation.TRANSITION.search(x) for x in texts), texts


def test_review_state_machine():
    assert review.next_status("submit", review.GENERATED) == review.PENDING_REVIEW
    assert review.next_status("approve", review.PENDING_REVIEW) == review.APPROVED
    assert review.next_status("edit", review.APPROVED) == review.REVISED
    assert review.next_status("request_changes", review.PENDING_REVIEW) == review.CHANGES_REQUESTED
    assert review.next_status("reopen", review.REJECTED) == review.PENDING_REVIEW
    with pytest.raises(review.ReviewError):
        review.next_status("approve", review.REJECTED)
    with pytest.raises(review.ReviewError):
        review.next_status("approve", review.GENERATED)  # nothing skips the queue
    assert "approve" not in review.allowed_decisions(review.CHANGES_REQUESTED)
