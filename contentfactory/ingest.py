"""Turn whatever transcript we were given into the sentence-level IR (see `ir.py`).

Accepted inputs:
  * MeetStream segment-list transcripts (`speaker`, `transcript`, `start_time`, `end_time`, `words`)
  * MeetStream participant-keyed transcripts (`participant.name`, `words[].start_timestamp.relative`)
  * Our own IR JSON (`{"webinar_id": ..., "segments": [...]}`), e.g. a re-imported transcript.json
  * SRT / WebVTT, with an optional `Speaker: ` prefix (or `<v Speaker>` voice tag) on each cue
  * Media with no transcript, via faster-whisper when it is installed

Every input goes through the same two steps:

1. **Parse** into `_Utterance`s: one speaker turn each, with times and (if available) words.
2. **Split** each utterance into sentence-sized `Segment`s. With word timings we split at
   words ending in `.?!`, so every segment's times are exact. Without them we split the text
   with a regex and share the utterance's time span out in proportion to sentence length,
   which is approximate but good enough for citations and captions.

Segments are capped at `MAX_SEGMENT_SECONDS` so that a long unpunctuated monologue (common in
auto-generated transcripts) still yields citable, clip-sized pieces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .ir import Segment, Transcript, Word

MAX_SEGMENT_SECONDS = 25.0
# A word that ends a sentence: "done." / "really?" / "(see above)."
_SENTENCE_END = re.compile(r"[.!?]['\")\]]*$")
# A sentence break inside running text: terminal punctuation, whitespace, then a capital/digit/quote.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])['\")\]]*\s+(?=[A-Z0-9\"'(])")


class TranscriptError(ValueError):
    """The transcript can't be used. The message is shown to the user as the job's error."""


class _Utterance:
    """One speaker turn, in a common shape regardless of the source format."""

    __slots__ = ("speaker", "text", "start", "end", "words")

    def __init__(self, speaker: str, text: str, start: float, end: float, words: list[Word]):
        self.speaker, self.text, self.start, self.end, self.words = speaker, text, start, end, words


# ---------------------------------------------------------------- format parsers


def _num(d: dict, *keys: str) -> float | None:
    """First numeric value among `keys`. Providers disagree on field names, so we try several."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):  # participant-keyed timestamps: {"relative": 1.2, "absolute": ...}
            v = v.get("relative")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _parse_words(raw_words: list[dict]) -> list[Word]:
    """Word timings from either MeetStream format. Prefers `punctuated_word` so sentence ends are visible."""
    words = []
    for w in raw_words or []:
        text = w.get("punctuated_word") or w.get("text") or w.get("word") or ""
        start = _num(w, "start", "start_timestamp")
        end = _num(w, "end", "end_timestamp")
        if text and start is not None and end is not None:
            words.append(Word(text=text, start=start, end=end, confidence=w.get("confidence")))
    return words


def _utterances_from_json(data: Any) -> list[_Utterance]:
    """Parse any of the JSON transcript shapes we know into utterances.

    Accepts a bare list of utterances, or an object wrapping one under `segments`,
    `transcript`, `utterances` or `results`. Items without text or timing are skipped.
    """
    if isinstance(data, dict):
        for key in ("segments", "transcript", "utterances", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise TranscriptError("Unrecognised transcript JSON: expected a list of utterances.")
    if not isinstance(data, list):
        raise TranscriptError("Unrecognised transcript JSON: expected a list of utterances.")

    out: list[_Utterance] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Speaker name: participant-keyed format puts it under `participant.name`; the
        # segment-list format uses `speaker`, which some providers give as an integer index.
        participant = item.get("participant") or {}
        speaker = (
            item.get("speaker_name")
            or (participant.get("name") if isinstance(participant, dict) else None)
            or item.get("speaker")
            or "Speaker"
        )
        speaker = str(speaker) if not isinstance(speaker, int) else f"Speaker {speaker + 1}"
        words = _parse_words(item.get("words", []))
        text = item.get("transcript") or item.get("text") or " ".join(w.text for w in words)
        start = _num(item, "start_time", "start")
        end = _num(item, "end_time", "end")
        if words:  # participant-keyed items have no utterance-level times; derive them from words
            start = words[0].start if start is None else start
            end = words[-1].end if end is None else end
        if not text or start is None or end is None:
            continue
        out.append(_Utterance(speaker, text.strip(), start, max(end, start), words))
    return out


# "00:01:02,500 --> 00:01:05,000" (SRT) or "01:02.500 --> 01:05.000" (VTT, hours optional)
_CUE_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


def _t(h, m, s, ms) -> float:
    """Cue timestamp parts to seconds. `ms` may be 1-3 digits ("5" means 500 ms)."""
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _utterances_from_cues(text: str) -> list[_Utterance]:
    """SRT or WebVTT cues to utterances. Cue numbers, headers and NOTE blocks are ignored.

    A leading `Name:` or `<v Name>` becomes the speaker; otherwise the speaker is "Speaker".
    """
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            m = _CUE_TIME.search(line)
            if not m:
                continue
            start, end = _t(*m.groups()[:4]), _t(*m.groups()[4:])
            body = " ".join(lines[i + 1 :])
            speaker = "Speaker"
            sm = re.match(r"^<v\s+([^>]{1,40})>\s*(.*)$", body) or re.match(r"^([A-Z][\w .'-]{0,40}?):\s+(.*)$", body)
            if sm:
                speaker, body = sm.group(1).strip(), sm.group(2)
            body = re.sub(r"<[^>]+>", "", body).strip()  # drop VTT styling tags like <i> or <c.red>
            if body:
                out.append(_Utterance(speaker, body, start, end, []))
            break
    return out


# ---------------------------------------------------------------- sentence splitting


def _split_with_words(u: _Utterance) -> list[tuple[str, float, float, list[Word]]]:
    """Split at sentence-ending words (or every MAX_SEGMENT_SECONDS). Times are exact."""
    pieces, current = [], []
    for w in u.words:
        current.append(w)
        long_enough = current[-1].end - current[0].start >= MAX_SEGMENT_SECONDS
        if _SENTENCE_END.search(w.text) or long_enough:
            pieces.append(current)
            current = []
    if current:
        pieces.append(current)
    return [(" ".join(w.text for w in p), p[0].start, p[-1].end, p) for p in pieces]


def _split_proportionally(u: _Utterance) -> list[tuple[str, float, float, list[Word]]]:
    """Split text into sentences and share the utterance's time span by character count.

    Approximate: assumes a constant speaking rate within one utterance.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(u.text) if s.strip()]
    if len(sentences) <= 1:
        return [(u.text, u.start, u.end, [])]
    total = sum(len(s) for s in sentences)
    span, t = u.end - u.start, u.start
    out = []
    for s in sentences:
        d = span * len(s) / total
        out.append((s, t, t + d, []))
        t += d
    return out


def build_transcript(webinar_id: str, utterances: list[_Utterance], duration: float | None) -> Transcript:
    """Utterances to a Transcript with ids `s0001`... in time order.

    `duration` is the recording's length when we have the media; the transcript may end
    before the recording does (silence at the end), and clips may not run past the media.
    """
    utterances = sorted(utterances, key=lambda u: u.start)
    segments: list[Segment] = []
    for u in utterances:
        pieces = _split_with_words(u) if u.words else _split_proportionally(u)
        for text, start, end, words in pieces:
            confs = [w.confidence for w in words if w.confidence is not None]
            segments.append(
                Segment(
                    id=f"s{len(segments) + 1:04d}",
                    start=round(start, 3),
                    end=round(max(end, start + 0.2), 3),  # never zero-length, even for one-word cues
                    speaker=u.speaker,
                    text=text,
                    words=words,
                    confidence=round(sum(confs) / len(confs), 3) if confs else None,
                )
            )
    if not segments:
        raise TranscriptError("The transcript contains no speech.")
    end = max(s.end for s in segments)
    return Transcript(webinar_id=webinar_id, duration=max(duration or 0, end), segments=segments)


# ---------------------------------------------------------------- entry points


def parse_transcript(webinar_id: str, raw: str | bytes, filename: str = "", duration: float | None = None) -> Transcript:
    """Parse a transcript file or API response. The format is detected from the filename and content."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    name = filename.lower()
    if name.endswith((".srt", ".vtt")) or text.lstrip().startswith("WEBVTT"):
        utterances = _utterances_from_cues(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise TranscriptError(f"Transcript is neither JSON nor SRT/VTT: {e}") from e
        if isinstance(data, dict) and "segments" in data and data.get("webinar_id"):
            # Our own IR (e.g. a re-imported transcript.json): keep ids as they are.
            data["webinar_id"] = webinar_id
            return Transcript.model_validate(data)
        utterances = _utterances_from_json(data)
    return build_transcript(webinar_id, utterances, duration)


def transcribe_media(webinar_id: str, audio_path: Path, duration: float | None) -> Transcript:
    """Local speech-to-text for uploads that arrive without a transcript.

    Optional (`pip install faster-whisper`). Uses the small "base" model on CPU or GPU. It
    has no speaker diarisation, so everything is attributed to "Speaker"; for real webinars,
    MeetStream's own transcript (with speaker names) is much better input.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise TranscriptError(
            "No transcript was provided and faster-whisper is not installed. Upload a transcript "
            "(MeetStream JSON, SRT or VTT) with the recording, or `pip install faster-whisper`."
        ) from e
    model = WhisperModel("base", device="auto", compute_type="int8")
    raw_segments, _info = model.transcribe(str(audio_path), word_timestamps=True, vad_filter=True)
    utterances = []
    for s in raw_segments:
        words = [Word(text=w.word.strip(), start=w.start, end=w.end, confidence=w.probability) for w in s.words or []]
        utterances.append(_Utterance("Speaker", s.text.strip(), s.start, s.end, words))
    return build_transcript(webinar_id, utterances, duration)
