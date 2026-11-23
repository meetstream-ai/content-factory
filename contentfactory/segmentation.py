"""Chapter boundary detection without an LLM.

Why not ask the LLM for chapters? Because the boundaries then change on every run, drift away
from real sentence starts, and are hard to test. Here boundaries come from a TextTiling-style
algorithm and the LLM only *names* the chapters afterwards (see `pipeline._stage_chapters`).

How it works, in four steps:

1. **Blocks.** The transcript is cut into fixed-duration blocks (20-60 s, scaled so a
   recording has roughly 40 of them). Blocks always end on a segment boundary.
2. **Similarity curve.** For every gap between two blocks, the vocabulary of the
   `WINDOW_BLOCKS` blocks before the gap is compared with the blocks after it (TF-IDF
   weighted cosine similarity). A topic change shows up as a dip in similarity.
3. **Depth scores.** Each gap gets a "depth": how far its similarity falls below the
   highest point on either side. Spoken transitions ("moving on to", "let's talk about")
   add a bonus, as does a change of speaker.
4. **Selection.** The deepest gaps become chapter starts, subject to a maximum count and a
   minimum spacing, and each one is nudged onto the nearest transition sentence.

The result is a list of segment indices, so chapters always begin on a real sentence.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from .ir import Segment, Transcript

# How many blocks on each side of a gap are pooled when comparing vocabulary. Larger values
# smooth out noise but blur short sections.
WINDOW_BLOCKS = 3

# Score bonuses added to a gap's depth (depths themselves are roughly 0-1).
TRANSITION_BONUS = 0.25  # the block after the gap opens with a spoken transition
SPEAKER_CHANGE_BONUS = 0.05  # a different person starts talking at the gap
MIN_DEPTH = 0.05  # gaps shallower than this are never chapter boundaries


def block_seconds(duration: float) -> float:
    """Block length for a recording: ~40 blocks in total, clamped to 20-60 seconds.

    A 4-minute call gets 20 s blocks (so short sections are still visible); an hour-long
    webinar gets 60 s blocks (so a single tangent doesn't look like a new chapter).
    """
    return max(20.0, min(60.0, duration / 40))


# Phrases speakers use when they change topic. Matched against the start of a segment.
# Also used by validate.resolve_clip so clips end before the speaker moves on.
TRANSITION = re.compile(
    r"^(so,? )?(ok(ay)?,? |alright,? |all right,? )?(moving on|next,|now,? let's|let's (talk|move|look|dig|turn|get)"
    r"|let me (show|walk|turn)|the next (thing|part|topic|section)|our next|which brings (me|us)"
    r"|to wrap up|in closing|before we (wrap|close|finish)|let's open (it )?up|questions)",
    re.I,
)

# Words that carry no topic information. Kept deliberately broad: spoken English is full of
# filler ("gonna", "kind of", "basically") that would otherwise dominate the counts.
STOPWORDS = set(
    """a about above after again against all am an and any are as at be because been before being below between
    both but by can could did do does doing down during each few for from further had has have having he her here
    hers herself him himself his how i if in into is it its itself just let lets me more most my myself no nor not
    now of off on once only or other our ours ourselves out over own really right same she should so some such
    than that the their theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you your yours yourself yourselves
    going gonna get got thing things kind sort like okay ok yeah um uh actually basically also one two well know
    think want need make way see go lot say said mean us im youre dont thats its theres were weve ive ill""".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercased content words of 3+ letters, stopwords removed. Shared with the offline generator."""
    words = re.findall(r"[a-z][a-z0-9'-]+", text.lower().replace("’", "'"))
    return [t for t in words if t.replace("'", "") not in STOPWORDS and len(t) > 2]


def _blocks(segments: list[Segment], size: float) -> list[list[Segment]]:
    """Group consecutive segments into blocks of at least `size` seconds."""
    blocks, current = [], []
    for s in segments:
        current.append(s)
        if s.end - current[0].start >= size:
            blocks.append(current)
            current = []
    if current:
        # A short leftover tail is merged into the previous block rather than becoming a
        # tiny block of its own, which would produce a noisy similarity value.
        if blocks and current[-1].end - current[0].start < size / 3:
            blocks[-1].extend(current)
        else:
            blocks.append(current)
    return blocks


def _cosine(a: Counter, b: Counter) -> float:
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def target_chapter_count(duration: float) -> int:
    """Upper bound on chapters: 2 x sqrt(minutes), clamped to 3-9.

    About 4 for a 4-minute call, 6 for 10 minutes, 9 for 20+ minutes. It is a ceiling, not a
    target: gaps that aren't deep enough are never used, so a single-topic talk gets fewer.
    """
    return max(3, min(9, round(2 * (duration / 60) ** 0.5)))


def detect_boundaries(transcript: Transcript, n_chapters: int | None = None) -> list[int]:
    """Segment indices where chapters start. Always includes 0 (the first chapter)."""
    segs = transcript.segments
    blocks = _blocks(segs, block_seconds(transcript.duration))
    if len(blocks) < 3:
        return [0]  # too short to have a meaningful topic structure
    n_chapters = n_chapters or target_chapter_count(transcript.duration)

    # TF-IDF: words used in every block (usually the webinar's own topic, e.g. "agents")
    # say nothing about *where* the topic changes, so they are down-weighted.
    block_tokens = [Counter(tokenize(" ".join(s.text for s in b))) for b in blocks]
    df = Counter(t for c in block_tokens for t in c)
    idf = {t: math.log((1 + len(blocks)) / (1 + n)) + 1 for t, n in df.items()}
    weighted = [Counter({t: v * idf[t] for t, v in c.items()}) for c in block_tokens]

    # sims[i] is the similarity across the gap between block i and block i+1.
    sims = []
    for gap in range(1, len(blocks)):
        left = sum(weighted[max(0, gap - WINDOW_BLOCKS) : gap], Counter())
        right = sum(weighted[gap : gap + WINDOW_BLOCKS], Counter())
        sims.append(_cosine(left, right))

    scores = []
    for i, sim in enumerate(sims):
        # Depth = how far this gap dips below the highest similarity on each side of it.
        lpeak = max(sims[: i + 1])
        rpeak = max(sims[i:])
        depth = (lpeak - sim) + (rpeak - sim)
        first = blocks[i + 1][0]  # first segment after the gap
        prev = blocks[i][-1]  # last segment before it
        if TRANSITION.search(first.text):
            depth += TRANSITION_BONUS
        if first.speaker != prev.speaker:
            depth += SPEAKER_CHANGE_BONUS
        scores.append((depth, i + 1))

    # Chapters shorter than this are not useful for viewers (YouTube also needs >= 10 s).
    min_gap_seconds = max(30.0, transcript.duration / (n_chapters * 2.5))
    chosen: list[int] = []
    for depth, block_idx in sorted(scores, reverse=True):  # deepest gaps first
        if len(chosen) >= n_chapters - 1 or depth <= MIN_DEPTH:
            break
        t = blocks[block_idx][0].start
        too_close = t < min_gap_seconds or transcript.duration - t < min_gap_seconds
        too_close = too_close or any(abs(t - blocks[c][0].start) < min_gap_seconds for c in chosen)
        if not too_close:
            chosen.append(block_idx)

    starts = sorted({0, *(transcript.index(blocks[b][0].id) for b in chosen)})
    return [_snap_to_transition(segs, i) for i in starts]


def _snap_to_transition(segs: list[Segment], idx: int, reach: int = 3) -> int:
    """Move a boundary onto a nearby segment that opens with a transition phrase or a new speaker.

    Block edges are arbitrary (they fall every N seconds), so the true topic change is usually a
    sentence or two away. Looks up to `reach` segments in each direction; transition phrases win
    over speaker changes; if neither is nearby the boundary stays where it was.
    """
    if idx == 0:
        return 0
    for j in range(max(1, idx - reach), min(len(segs), idx + reach + 1)):
        if TRANSITION.search(segs[j].text):
            return j
    for j in range(max(1, idx - reach), min(len(segs), idx + reach + 1)):
        if segs[j].speaker != segs[j - 1].speaker:
            return j
    return idx


def chapter_spans(transcript: Transcript, starts: list[int]) -> list[tuple[int, int]]:
    """Turn chapter start indices into inclusive (first, last) segment index pairs."""
    starts = sorted(set(starts))
    ends = [s - 1 for s in starts[1:]] + [len(transcript.segments) - 1]
    return list(zip(starts, ends))


def top_keywords(segments: list[Segment], k: int = 5, background: Counter | None = None) -> list[str]:
    """The `k` most frequent content words in `segments`.

    With `background` (word counts for the whole recording), words that are common everywhere
    are penalised, so a chapter's keywords describe what is distinctive about that chapter.
    """
    counts = Counter(tokenize(" ".join(s.text for s in segments)))
    if background:
        total = sum(background.values()) or 1
        counts = Counter({t: v / (1 + 50 * background[t] / total) for t, v in counts.items()})
    return [t for t, _ in counts.most_common(k)]
