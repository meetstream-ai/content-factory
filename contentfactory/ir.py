"""The intermediate representation (IR) that every generated asset points back into.

The whole product rests on one idea: **timestamps are data, not prose**.

A transcript is stored as a list of sentence-sized `Segment`s with stable ids
(`s0001`, `s0002`, ...). Generators (Claude or the offline heuristics) never write
timestamps. Wherever they need to say "this came from the recording", they cite segment
ids, and code in this module turns those ids into times. So:

  * a source reference can't point at a moment that doesn't exist (unknown ids are dropped
    and flagged, see `validate.resolve_refs`);
  * clips start and end on real sentence boundaries;
  * the review UI can turn any citation into a "jump to this moment" button.

The transcript is saved per webinar as `transcript.json` and shipped inside every export.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, PrivateAttr


class Word(BaseModel):
    """One spoken word with timing (seconds from the start of the recording)."""

    text: str
    start: float
    end: float
    confidence: float | None = None  # 0-1 from the speech-to-text provider, when it gives one


class Segment(BaseModel):
    """One sentence (or a <=25 s run of speech with no sentence break) by one speaker."""

    id: str  # "s0001", ... ; stable for the life of the webinar
    start: float
    end: float
    speaker: str
    text: str
    words: list[Word] = Field(default_factory=list)  # empty when the source had no word timings
    confidence: float | None = None  # mean word confidence

    @property
    def duration(self) -> float:
        return self.end - self.start


class SourceRef(BaseModel):
    """Where in the recording an asset (or part of one) came from.

    Stored on every asset as `source_references`, in this same shape for every asset type,
    so the UI and the export manifest handle them uniformly.
    """

    webinar_id: str
    start_seconds: float
    end_seconds: float
    speaker: str | None = None  # None when the referenced span has more than one speaker
    segment_ids: list[str] = Field(default_factory=list)


class Transcript(BaseModel):
    webinar_id: str
    duration: float  # of the recording if known, otherwise the end of the last segment
    segments: list[Segment]
    _by_id: dict[str, int] = PrivateAttr(default_factory=dict)  # segment id -> list index

    def model_post_init(self, _ctx) -> None:
        self._by_id = {s.id: i for i, s in enumerate(self.segments)}

    def index(self, segment_id: str) -> int:
        """Position of a segment in `segments`. Raises KeyError for unknown ids."""
        return self._by_id[segment_id]

    def has(self, segment_id: str) -> bool:
        return segment_id in self._by_id

    def get(self, segment_id: str) -> Segment:
        return self.segments[self._by_id[segment_id]]

    def span(self, first_id: str, last_id: str) -> list[Segment]:
        """All segments from `first_id` to `last_id` inclusive (order of the ids doesn't matter)."""
        i, j = self.index(first_id), self.index(last_id)
        if i > j:
            i, j = j, i
        return self.segments[i : j + 1]

    def ref(self, segment_ids: list[str]) -> SourceRef | None:
        """A SourceRef covering the given segment ids. Unknown ids are dropped; None if none remain."""
        known = sorted({sid for sid in segment_ids if self.has(sid)}, key=self.index)
        if not known:
            return None
        segs = [self.get(sid) for sid in known]
        speakers = {s.speaker for s in segs}
        return SourceRef(
            webinar_id=self.webinar_id,
            start_seconds=segs[0].start,
            end_seconds=max(s.end for s in segs),
            speaker=speakers.pop() if len(speakers) == 1 else None,
            segment_ids=known,
        )

    @property
    def speakers(self) -> list[str]:
        """Speaker names in order of first appearance."""
        seen: dict[str, None] = {}
        for s in self.segments:
            seen.setdefault(s.speaker, None)
        return list(seen)

    def render_for_llm(self) -> str:
        """The transcript as Claude sees it: one line per segment, `[s0042 13:54-14:02] Speaker: text`.

        The id at the start of each line is what the model cites. Times are included so the
        model can judge clip lengths, but it never returns them.
        """
        return "\n".join(
            f"[{s.id} {fmt_clock(s.start)}-{fmt_clock(s.end)}] {s.speaker}: {s.text}" for s in self.segments
        )


def fmt_clock(seconds: float) -> str:
    """Compact clock for display and YouTube chapters: `04:20`, or `1:04:20` past an hour."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_hms(seconds: float) -> str:
    """Fixed-width `HH:MM:SS`, used in exported metadata (e.g. a quote's `timestamp`)."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s']")


def normalize_text(text: str) -> str:
    """Lowercase, straight quotes, punctuation removed, single spaces.

    Used to check that a "verbatim" quote really is verbatim: two strings that differ only
    in capitalisation, punctuation or curly quotes normalise to the same thing, but any change
    to the words themselves does not.
    """
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return _WS.sub(" ", _NON_WORD.sub(" ", text.lower())).strip()
