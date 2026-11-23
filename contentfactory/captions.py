"""SRT / WebVTT captions for a clip, timed relative to the clip's own start.

Captions are built from word timings: from the transcript when the provider supplied them
(MeetStream does), otherwise spread evenly across each segment. Words are grouped into
short cues that are comfortable to read on a phone:

  * at most `MAX_WORDS_PER_CUE` words and `MAX_CUE_SECONDS` seconds per cue;
  * a new cue after a sentence ends, or after a pause of more than 0.8 s.

The same cues produce both files: SRT for editors and social uploads, VTT for browser
players. The SRT is also what FFmpeg burns into the video (see `media.render_clip`).
"""

from __future__ import annotations

from .ir import Segment, Word

MAX_WORDS_PER_CUE = 7
MAX_CUE_SECONDS = 3.5
PAUSE_SPLIT_SECONDS = 0.8

Cue = tuple[float, float, str]  # (start, end, text), seconds from the start of the clip


def _timed_words(segments: list[Segment]) -> list[Word]:
    """Word timings from the transcript when present, else spread evenly across each segment."""
    words: list[Word] = []
    for seg in segments:
        if seg.words:
            words.extend(seg.words)
            continue
        tokens = seg.text.split()
        if not tokens:
            continue
        step = seg.duration / len(tokens)
        words.extend(
            Word(text=t, start=seg.start + i * step, end=seg.start + (i + 1) * step) for i, t in enumerate(tokens)
        )
    return words


def build_cues(segments: list[Segment], clip_start: float, clip_end: float) -> list[Cue]:
    """Cues for the words spoken between `clip_start` and `clip_end` (recording time)."""
    words = [w for w in _timed_words(segments) if w.end > clip_start and w.start < clip_end]
    cues: list[Cue] = []
    current: list[Word] = []

    def flush():
        if current:
            # Clamp to the clip window and shift so the clip's first frame is t=0.
            start = max(current[0].start, clip_start) - clip_start
            end = min(current[-1].end, clip_end) - clip_start
            cues.append((round(start, 3), round(max(end, start + 0.3), 3), " ".join(w.text for w in current)))
            current.clear()

    for w in words:
        if current and (
            len(current) >= MAX_WORDS_PER_CUE
            or w.end - current[0].start > MAX_CUE_SECONDS
            or w.start - current[-1].end > PAUSE_SPLIT_SECONDS
        ):
            flush()
        current.append(w)
        if w.text.endswith((".", "?", "!")):
            flush()
    flush()
    return cues


def _stamp(t: float, sep: str) -> str:
    """Seconds to `HH:MM:SS<sep>mmm` (SRT uses a comma, VTT a dot)."""
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    return "\n".join(
        f"{i}\n{_stamp(a, ',')} --> {_stamp(b, ',')}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1)
    )


def to_vtt(cues: list[Cue]) -> str:
    body = "\n".join(f"{_stamp(a, '.')} --> {_stamp(b, '.')}\n{text}\n" for a, b, text in cues)
    return "WEBVTT\n\n" + body
