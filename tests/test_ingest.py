import json

import pytest

from contentfactory import captions
from contentfactory.ingest import TranscriptError, parse_transcript


def test_meetstream_segment_list_is_split_into_sentences(transcript):
    assert len(transcript.segments) == 10
    first = transcript.segments[0]
    assert first.id == "s0001" and first.speaker == "Maya Chen"
    assert first.text.startswith("Welcome everyone")
    assert first.start == 0.0 and first.end == pytest.approx(4.0, abs=0.01)
    assert first.confidence == pytest.approx(0.95)
    # Each sentence keeps its own word timings, so captions and clips are exact.
    third = transcript.get("s0003")
    assert third.speaker == "Dev Patel" and third.words[0].text == "The"
    assert transcript.speakers == ["Maya Chen", "Dev Patel"]


def test_meetstream_participant_keyed_format():
    data = [{
        "participant": {"id": 100, "name": "Ana"},
        "words": [{"text": "Hello", "start_timestamp": {"relative": 1.0}, "end_timestamp": {"relative": 1.4}},
                  {"text": "there.", "start_timestamp": {"relative": 1.4}, "end_timestamp": {"relative": 1.8}},
                  {"text": "Next", "start_timestamp": {"relative": 2.0}, "end_timestamp": {"relative": 2.3}},
                  {"text": "point.", "start_timestamp": {"relative": 2.3}, "end_timestamp": {"relative": 2.7}}],
    }]
    t = parse_transcript("w", json.dumps(data))
    assert [s.text for s in t.segments] == ["Hello there.", "Next point."]
    assert t.segments[1].start == 2.0 and t.segments[0].speaker == "Ana"


def test_meetstream_transcript_without_word_timings_is_split_proportionally():
    # Some transcription providers return utterances without words; sentence times are then shared out.
    data = [{"speaker": "Dev", "transcript": "First idea here. Second idea follows.", "start_time": 1.0, "end_time": 9.0},
            {"speaker": "Maya", "transcript": "Thanks.", "start_time": 10.0, "end_time": 12.0}]
    t = parse_transcript("w", json.dumps(data))
    assert [s.speaker for s in t.segments] == ["Dev", "Dev", "Maya"]
    assert t.segments[0].text == "First idea here."
    assert t.segments[1].end == pytest.approx(9.0)


def test_non_json_transcript_is_a_clear_error():
    with pytest.raises(TranscriptError, match="not valid JSON"):
        parse_transcript("w", "1\n00:00:01,000 --> 00:00:02,000\nHi.\n")


def test_empty_transcript_is_a_clear_error():
    with pytest.raises(TranscriptError, match="no speech"):
        parse_transcript("w", "[]")


def test_captions_are_relative_to_clip_and_well_formed(transcript):
    segs = transcript.span("s0003", "s0004")
    cues = captions.build_cues(segs, segs[0].start, segs[-1].end)
    assert cues[0][0] == 0.0
    assert all(b > a for a, b, _ in cues)
    assert all(len(text.split()) <= captions.MAX_WORDS_PER_CUE for *_, text in cues)
    srt = captions.to_srt(cues)
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert captions.to_vtt(cues).startswith("WEBVTT\n\n00:00:00.000 --> ")
    assert " ".join(text for *_, text in cues) == " ".join(s.text for s in segs)
