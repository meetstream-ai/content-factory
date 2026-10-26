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
    t = parse_transcript("w", json.dumps(data), "t.json")
    assert [s.text for s in t.segments] == ["Hello there.", "Next point."]
    assert t.segments[1].start == 2.0 and t.segments[0].speaker == "Ana"


def test_srt_with_speaker_prefix_and_proportional_split():
    srt = "1\n00:00:01,000 --> 00:00:09,000\nDev: First idea here. Second idea follows.\n\n2\n00:00:10,000 --> 00:00:12,000\nThanks.\n"
    t = parse_transcript("w", srt, "talk.srt")
    assert [s.speaker for s in t.segments] == ["Dev", "Dev", "Speaker"]
    assert t.segments[0].text == "First idea here."
    assert t.segments[1].end == pytest.approx(9.0)


def test_vtt_voice_tags_set_the_speaker():
    vtt = "WEBVTT\n\n00:01.000 --> 00:04.000\n<v Maya Chen>Hello <i>there</i>.\n"
    t = parse_transcript("w", vtt, "talk.vtt")
    assert (t.segments[0].speaker, t.segments[0].text) == ("Maya Chen", "Hello there.")


def test_empty_transcript_is_a_clear_error():
    with pytest.raises(TranscriptError, match="no speech"):
        parse_transcript("w", "[]", "t.json")


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
