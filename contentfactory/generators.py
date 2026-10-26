"""Content generators. Both implement the `Generator` protocol and return the draft models in drafts.py.

ClaudeGenerator   the real thing: Claude via the Anthropic SDK, with structured outputs.
OfflineGenerator  deterministic heuristics, for tests and for demos without an API key.
                  It picks and arranges the speakers' own sentences; it doesn't write.

What a generator is (and isn't) responsible for:
  * It decides *what to say*: chapter names, which moments make good clips, the brief,
    the blog, posts, quotes, design briefs.
  * It cites transcript segment ids for everything it claims.
  * It is NOT trusted for facts the code can check. Clip boundaries, quote wording,
    citations and scores are all re-checked in validate.py, and anything that fails is
    dropped or flagged, whichever generator produced it.

How the Claude calls are built:
  * System prompt = fixed instructions + the full transcript. The transcript block carries
    `cache_control`, so all seven-plus calls in a job read it from the prompt cache instead
    of paying for it again (and later calls start faster).
  * Each stage's specific instructions, chapter table and content brief go in the user
    message, after the cached prefix.
  * Output shape is enforced with structured outputs (`output_format=<Pydantic model>`), so
    there is no JSON parsing or retry-on-bad-JSON code.
  * Adaptive thinking with a configurable effort (default "medium"; see CF_EFFORT).
  * Server-side refusal fallback (`fallbacks: "default"`) is on, so a safety classifier
    false positive is retried on a fallback model instead of failing the stage.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

from .drafts import (
    BlogDraft, BlogSection, ChapterName, ChapterNames, ClipCandidate, ClipCandidates, ContentBrief,
    EditorialTakeaway, JobConfig, LinkedInPosts, Paragraph, QuoteSet, Slide, SocialPost, Takeaway,
    VerbatimQuote, VisualBrief, VisualBriefs,
)
from .ir import Segment, Transcript, fmt_clock
from .segmentation import tokenize, top_keywords

T = TypeVar("T", bound=BaseModel)


class GenerationError(RuntimeError):
    """A generation step failed; the message says why (refusal, auth, rate limit, truncation)."""


@dataclass
class Chapter:
    """A chapter during generation: segment index range plus (once named) title, summary, keywords."""

    index: int
    first: int  # segment index
    last: int
    title: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class GenContext:
    """Inputs common to every generator call."""

    title: str
    transcript: Transcript
    config: JobConfig
    chapters: list[Chapter] = field(default_factory=list)

    def chapter_table(self) -> str:
        """Chapters as prompt text: `2. Title [s0040..s0071, 04:20-12:35] - summary`."""
        segs = self.transcript.segments
        return "\n".join(
            f"{c.index}. {c.title or '(untitled)'} [{segs[c.first].id}..{segs[c.last].id}, "
            f"{fmt_clock(segs[c.first].start)}-{fmt_clock(segs[c.last].end)}]" + (f" - {c.summary}" if c.summary else "")
            for c in self.chapters
        )


class Generator(Protocol):
    """The interface the pipeline calls. Each method returns a Pydantic draft (see drafts.py)."""

    name: str  # recorded on the job and as the author of version 1 of each asset

    def name_chapters(self, ctx: GenContext) -> ChapterNames: ...
    def find_clips(self, ctx: GenContext) -> ClipCandidates: ...
    def brief(self, ctx: GenContext) -> ContentBrief: ...
    def blog(self, ctx: GenContext, brief: ContentBrief) -> BlogDraft: ...
    def linkedin(self, ctx: GenContext, brief: ContentBrief) -> LinkedInPosts: ...
    def quotes(self, ctx: GenContext, brief: ContentBrief) -> QuoteSet: ...
    def visuals(self, ctx: GenContext, brief: ContentBrief, quotes: list[dict]) -> VisualBriefs: ...
    def revise(self, ctx: GenContext, brief: ContentBrief | None, schema: type[T], previous: dict, feedback: str) -> T: ...


# How the job's `audience` setting is described to the model.
AUDIENCE = {
    "developers": "software developers and AI engineers",
    "founders": "startup founders and technical leaders",
    "marketers": "marketing professionals",
    "general": "a general professional audience",
}


# ===================================================================== Claude


# Shared by every call. Explains the citation contract and the no-invention rule once, with the
# reasons, so stage prompts can stay short and focused on the craft of each asset.
SYSTEM_PROMPT = """\
You are the content team for a company that just hosted a webinar. You turn the recording's transcript \
into marketing assets that a human editor will review before anything is published.

How the material works:
- The transcript below is the only source of truth. Each line is `[segment_id start-end] Speaker: text`.
- Wherever a field asks for segment ids, cite the ids of the lines that support the content. Cite only ids \
that appear in the transcript. An editor will click each citation to check it against the recording.
- Do not add facts, numbers, customer names, product capabilities or opinions the speakers did not express. \
Rephrasing and structuring are your job; inventing is not. If the webinar is thin on something, write less.
- Verbatim quotes must reproduce the speaker's words exactly (you may trim filler at the edges, nothing in the middle).
- Spoken transcripts contain filler, restarts and transcription errors. Write clean prose from them; don't \
imitate the speech patterns, and don't refer to "the transcript" in the assets themselves.
- When something asks for N items and only fewer are good, return fewer. Reviewers would rather approve three \
strong items than reject eight weak ones."""


class ClaudeGenerator:
    name = "claude"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        import anthropic

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic(max_retries=3)
        self.model = model
        self.effort = effort

    # ---------------------------------------------------------------- plumbing

    def _system(self, ctx: GenContext) -> list[dict]:
        """System blocks: instructions, then the transcript (cached). Identical for every stage of a job."""
        speakers = ", ".join(ctx.transcript.speakers)
        transcript = (
            f"Webinar: {ctx.title}\nDuration: {fmt_clock(ctx.transcript.duration)}\nSpeakers: {speakers}\n\n"
            f"<transcript>\n{ctx.transcript.render_for_llm()}\n</transcript>"
        )
        # The transcript block is identical across every stage of a job, so it is cached once and re-read.
        return [
            {"type": "text", "text": SYSTEM_PROMPT},
            {"type": "text", "text": transcript, "cache_control": {"type": "ephemeral"}},
        ]

    def _call(self, ctx: GenContext, schema: type[T], task: str, max_tokens: int = 16000) -> T:
        """One structured-output request. Returns a validated `schema` instance or raises GenerationError.

        Transient failures (429, 5xx, connection errors) are retried by the SDK itself
        (`max_retries=3` on the client); anything reaching the except blocks is final.
        """
        a = self._anthropic
        try:
            # Raw response first: on a refusal or truncation the body isn't valid JSON for the schema,
            # and parse() would raise a validation error that hides the real stop reason.
            raw = self.client.beta.messages.with_raw_response.parse(
                model=self.model,
                max_tokens=max_tokens,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=self._system(ctx),
                messages=[{"role": "user", "content": task}],
                output_format=schema,
            )
        except a.BadRequestError as e:
            raise GenerationError(f"Claude rejected the request: {e.message}") from e
        except (a.AuthenticationError, a.PermissionDeniedError) as e:
            raise GenerationError("Anthropic credentials are missing or invalid (set ANTHROPIC_API_KEY).") from e
        except a.RateLimitError as e:
            raise GenerationError("Rate limited by the Anthropic API after retries; retry the job later.") from e
        except a.APIStatusError as e:
            raise GenerationError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise GenerationError("Could not reach the Anthropic API.") from e

        body = raw.json()
        stop_reason = body.get("stop_reason")
        if stop_reason == "refusal":
            category = (body.get("stop_details") or {}).get("category")
            raise GenerationError(f"Claude declined this step (category: {category or 'unspecified'}).")
        if stop_reason == "max_tokens":
            raise GenerationError(f"Output for {schema.__name__} hit max_tokens before finishing.")
        try:
            parsed = raw.parse().parsed_output
        except ValueError as e:  # pydantic.ValidationError subclasses ValueError
            raise GenerationError(f"Claude's {schema.__name__} didn't match the schema: {e}") from e
        if parsed is None:
            raise GenerationError(f"Claude returned no parseable {schema.__name__}.")
        return parsed

    def _audience(self, ctx: GenContext) -> str:
        """The one-line audience/tone instruction added to most stage prompts."""
        return f"Target audience: {AUDIENCE[ctx.config.audience]}. Tone: {ctx.config.tone.replace('_', ' ')}."

    @staticmethod
    def _brief_text(brief: ContentBrief) -> str:
        """The content brief as prompt text, so every asset follows the same story."""
        return "<content_brief>\n" + brief.model_dump_json(indent=2) + "\n</content_brief>"

    # ---------------------------------------------------------------- stages

    def name_chapters(self, ctx: GenContext) -> ChapterNames:
        # Boundaries are fixed by segmentation.py; the model only names and summarises.
        task = (
            "Topic-shift detection has split the webinar into these chapters (segment ranges are fixed; do not change them):\n\n"
            f"{ctx.chapter_table()}\n\n"
            "Give each chapter a specific title (what a viewer learns there, e.g. 'Why Tool Calling Isn't Enough', "
            "not 'Discussion'), a one or two sentence summary, and 3-5 keywords. Use 'Introduction', 'Audience Q&A' "
            "or 'Closing Remarks' only when that is genuinely what the chapter is. Return one entry per chapter index."
        )
        return self._call(ctx, ChapterNames, task)

    def find_clips(self, ctx: GenContext) -> ClipCandidates:
        # The model proposes moments and sub-scores; validate.resolve_clip turns them into exact
        # times, and the weighted total score is computed in code, not by the model.
        c = ctx.config
        task = (
            f"{self._audience(ctx)}\n\nChapters:\n{ctx.chapter_table()}\n\n"
            f"Find up to {c.clip_count} moments that work as standalone short-form video clips, each "
            f"{c.min_clip_seconds:.0f}-{c.max_clip_seconds:.0f} seconds long (use the timestamps on each line).\n\n"
            "A good clip:\n"
            "- opens on its strongest line: a claim, a surprising number, a question, a named mistake. Not on "
            "'So, as I mentioned...', 'And another thing', or a pronoun whose referent is outside the clip;\n"
            "- states one complete idea and ends when that idea ends, not mid-explanation;\n"
            "- makes sense to someone who never saw the webinar;\n"
            "- teaches, surprises, or is quotable for this audience.\n\n"
            "Score each sub-metric 0-10 honestly; a reviewer compares scores against the clip. Clips must not overlap. "
            "Prefer spreading clips across chapters. Return fewer clips if fewer meet the bar."
        )
        return self._call(ctx, ClipCandidates, task)

    def brief(self, ctx: GenContext) -> ContentBrief:
        task = (
            f"{self._audience(ctx)}\n\nChapters:\n{ctx.chapter_table()}\n\n"
            "Write the shared content brief every downstream asset (blog, social posts, clips, graphics) will follow, "
            "so the campaign tells one coherent story. The primary message is the single idea the webinar argues. "
            "Give 3-5 key takeaways, each a sentence the speakers would agree with, citing the segments that support it. "
            "The call to action should point people at the full recording unless the speakers asked for something else."
        )
        return self._call(ctx, ContentBrief, task)

    def blog(self, ctx: GenContext, brief: ContentBrief) -> BlogDraft:
        c = ctx.config
        task = (
            f"{self._audience(ctx)}\n\n{self._brief_text(brief)}\n\nChapters:\n{ctx.chapter_table()}\n\n"
            f"Write a blog article of about {c.blog_word_count} words based on this webinar. It must read as an article, "
            "not a recap: organise it around the ideas (not the order things were said), with an introduction that "
            "sets up the problem, 3-6 sections with descriptive H2 headings, and a short conclusion. Every paragraph "
            "cites the segments that support it. Include an SEO title, a meta description under 160 characters, "
            "keywords, a one-paragraph excerpt, the key takeaways as short bullet sentences, and internal-link topics."
        )
        return self._call(ctx, BlogDraft, task, max_tokens=20000)

    def linkedin(self, ctx: GenContext, brief: ContentBrief) -> LinkedInPosts:
        n = ctx.config.linkedin_post_count
        task = (
            f"{self._audience(ctx)}\n\n{self._brief_text(brief)}\n\n"
            f"Write up to {n} LinkedIn posts from the company account, each on a different angle (a lesson, a "
            "contrarian point, a practical checklist, a question for discussion). LinkedIn style: a first line that "
            "earns the 'see more' click, short paragraphs, at most one list, 80-220 words, no emoji walls, 2-4 hashtags. "
            "End with a discussion prompt or a pointer to the full recording. Cite the supporting segments."
        )
        return self._call(ctx, LinkedInPosts, task)

    def quotes(self, ctx: GenContext, brief: ContentBrief) -> QuoteSet:
        n = ctx.config.quote_count
        task = (
            f"{self._audience(ctx)}\n\n{self._brief_text(brief)}\n\n"
            f"1. Pick up to {n} memorable verbatim quotes: lines that stand alone and would work on a quote card "
            "(roughly 8-35 words). Copy the words exactly as transcribed; give the id of the segment where the quote starts.\n"
            "2. Separately, write up to 5 editorial takeaways: punchy one-line rewrites of ideas from the webinar. These "
            "will be labelled as AI-written takeaways, never attributed to a speaker."
        )
        return self._call(ctx, QuoteSet, task)

    def visuals(self, ctx: GenContext, brief: ContentBrief, quotes: list[dict]) -> VisualBriefs:
        quote_lines = "\n".join(f"- [{q['segment_ids'][0]}] {q['speaker']}: \"{q['quote']}\"" for q in quotes) or "(none)"
        task = (
            f"{self._audience(ctx)}\n\n{self._brief_text(brief)}\n\nVerified verbatim quotes:\n{quote_lines}\n\n"
            "Write Canva-ready design briefs a designer can build without watching the webinar: up to 3 quote cards "
            "(1:1, using a verified quote above copied exactly, with the speaker), one LinkedIn carousel (4:5, 5-8 "
            "slides: title slide, one idea per slide, closing slide with the call to action), and one YouTube thumbnail "
            "concept (16:9). Give layout and visual direction concretely: hierarchy, imagery, mood. Cite segments."
        )
        return self._call(ctx, VisualBriefs, task)

    def revise(self, ctx: GenContext, brief: ContentBrief | None, schema: type[T], previous: dict, feedback: str) -> T:
        """A new version of one asset: the previous draft plus the reviewer's feedback."""
        import json

        task = (
            f"{self._audience(ctx)}\n\n" + (self._brief_text(brief) + "\n\n" if brief else "")
            + f"An editor reviewed this draft and requested changes.\n\n<draft>\n{json.dumps(previous, indent=2)}\n</draft>\n\n"
            f"<editor_feedback>\n{feedback}\n</editor_feedback>\n\n"
            "Produce the revised version. Apply the feedback fully, keep what the editor didn't ask to change, and keep "
            "citing supporting segments."
        )
        return self._call(ctx, schema, task, max_tokens=20000)


# ===================================================================== Offline
#
# The offline generator exists so the whole workflow (ingest, render, review, export) can be run
# and tested without an API key. Its heuristics are deliberately simple and transparent:
#   _HOOK      sentence openers that tend to grab attention ("The biggest...", "Never...")
#   _BACKREF   openers that depend on earlier context ("As I said...", "And...", "It...")
#   _INSIGHT   words that signal a lesson or opinion ("should", "mistake", "rule")
# Output quality is "a reviewer can exercise the buttons", not "ready to publish".


_HOOK = re.compile(
    r"^(the (biggest|most|real|first|one|single|hardest)|most (people|teams|developers)|here'?s|never|always|stop|"
    r"if you|what if|why |how |the mistake|the secret|nobody|everyone|you don'?t|don'?t|\d+)",
    re.I,
)
_BACKREF = re.compile(
    r"^(so,? as i (said|mentioned)|as i (said|mentioned)|and |but |also,? |which |that'?s why|it |this |they |"
    r"he |she |those |these |like i said|anyway|um|uh|yeah|right,? so)",
    re.I,
)
_INSIGHT = re.compile(r"\b(should|must|need|important|key|mistake|lesson|never|always|the reason|matters|trick|rule)\b", re.I)
_ANNOUNCED_TOPIC = re.compile(
    r"(?:talk about|look at|moving on to|move on to|turn to|dig into|discuss)\s+(?:the\s+)?(.{4,60}?)(?:\s+itself)?[.,?!]", re.I)
_SMALL_WORDS = {"a", "an", "and", "as", "at", "for", "in", "of", "on", "or", "the", "to", "with", "what", "how", "why"}


def _title_case(phrase: str) -> str:
    """Headline-style capitalisation that keeps acronyms (MCP, LLM) and lowercases small words."""
    words = phrase.strip().split()
    return " ".join(w if w.isupper() else (w.lower() if i and w.lower() in _SMALL_WORDS else w.capitalize())
                    for i, w in enumerate(words))


def _clean(text: str) -> str:
    """Strip filler ("um", "you know") and leading conjunctions from a spoken sentence."""
    text = re.sub(r"\b(um+|uh+|you know|i mean|kind of|sort of)\b,?\s*", "", text, flags=re.I)
    text = re.sub(r"^(so|and|but|okay|alright|yeah|right|well),?\s+", "", text.strip(), flags=re.I)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:1].upper() + text[1:] if text else text


class OfflineGenerator:
    name = "offline"

    def _sentences(self, ctx: GenContext, chapter: Chapter) -> list[Segment]:
        return ctx.transcript.segments[chapter.first : chapter.last + 1]

    def _insight_score(self, seg: Segment, background: set[str]) -> float:
        """How quotable/takeaway-worthy a sentence is: 0 for too short/long, more for hooks and insight words."""
        words = seg.text.split()
        if not 8 <= len(words) <= 40:
            return 0.0
        score = 2.0 if _INSIGHT.search(seg.text) else 0.0
        score += 1.5 if _HOOK.search(seg.text) else 0.0
        score -= 2.0 if _BACKREF.search(seg.text) else 0.0
        score += min(2.0, 0.4 * len(set(tokenize(seg.text)) & background))
        return score

    def _background(self, ctx: GenContext) -> set[str]:
        """The webinar's 25 main topic words, used to judge a sentence's relevance."""
        return set(top_keywords(ctx.transcript.segments, k=25))

    def name_chapters(self, ctx: GenContext) -> ChapterNames:
        whole = Counter(tokenize(" ".join(s.text for s in ctx.transcript.segments)))
        out = []
        n = len(ctx.chapters)
        for c in ctx.chapters:
            segs = self._sentences(ctx, c)
            text = " ".join(s.text for s in segs).lower()
            kws = top_keywords(segs, k=5, background=whole)
            if c.index == 0 and re.search(r"\b(welcome|introduc|my name|thanks for joining)\b", text):
                title = "Introduction"
            elif "question" in text[:400] and c.index >= n - 2:
                title = "Audience Q&A"
            elif c.index == n - 1 and re.search(r"\b(thank you|wrap up|closing|that's all)\b", text[-600:]):
                title = "Closing Remarks"
            elif m := _ANNOUNCED_TOPIC.search(segs[0].text):
                title = _title_case(m.group(1))
            else:
                title = " ".join(k.capitalize() for k in kws[:3]) or f"Chapter {c.index + 1}"
            first = next((_clean(s.text) for s in segs if len(s.text.split()) >= 6), _clean(segs[0].text))
            out.append(ChapterName(index=c.index, title=title, summary=first[:220], keywords=kws))
        return ChapterNames(chapters=out)

    def find_clips(self, ctx: GenContext) -> ClipCandidates:
        """Try every sentence as an opener, extend with the same speaker, score, keep the best non-overlapping."""
        cfg = ctx.config
        segs = ctx.transcript.segments
        background = self._background(ctx)
        candidates = []
        for i, first in enumerate(segs):
            if _BACKREF.search(first.text) or len(first.text.split()) < 5:
                continue
            j = i
            while j + 1 < len(segs) and segs[j + 1].speaker == first.speaker and segs[j + 1].end - first.start <= cfg.max_clip_seconds:
                j += 1
                if segs[j].end - first.start >= cfg.min_clip_seconds * 2:
                    break
            dur = segs[j].end - first.start
            if dur < cfg.min_clip_seconds:
                continue
            body = segs[i : j + 1]
            hook = 9 if _HOOK.search(first.text) else (6 if _INSIGHT.search(first.text) else 3)
            clarity = 8 if not re.match(r"^(it|this|that|they)\b", first.text, re.I) else 4
            edu = min(10, 3 + 2 * sum(bool(_INSIGHT.search(s.text)) for s in body))
            rel = min(10, 2 + len(set(tokenize(" ".join(s.text for s in body))) & background))
            eng = min(10, 3 + 3 * bool(re.search(r"\?|\d|mistake|never|wrong|myth", first.text, re.I)))
            candidates.append((hook * 25 + clarity * 25 + edu * 20 + rel * 15 + eng * 10, i, j, hook, clarity, edu, rel, eng))

        chosen, used = [], set()
        for _score, i, j, hook, clarity, edu, rel, eng in sorted(candidates, reverse=True):
            if any(k in used for k in range(i, j + 1)):
                continue
            used.update(range(i, j + 1))
            words = _clean(segs[i].text).rstrip(".").split()
            chosen.append(ClipCandidate(
                start_segment_id=segs[i].id, end_segment_id=segs[j].id,
                title=" ".join(words[:9]) + ("..." if len(words) > 9 else ""),
                hook_strength=hook, standalone_clarity=clarity, educational_value=edu,
                audience_relevance=rel, engagement_potential=eng,
                reason="Opens on a direct statement and stays with one speaker for the whole thought.",
                platforms=["linkedin", "youtube_shorts"],
            ))
            if len(chosen) >= cfg.clip_count:
                break
        return ClipCandidates(clips=chosen)

    def _best_per_chapter(self, ctx: GenContext, k: int = 1) -> list[tuple[Chapter, Segment]]:
        """The `k` most insightful sentences of each chapter (spreads takeaways across the talk)."""
        background = self._background(ctx)
        out = []
        for c in ctx.chapters:
            ranked = sorted(self._sentences(ctx, c), key=lambda s: self._insight_score(s, background), reverse=True)
            out.extend((c, s) for s in ranked[:k] if self._insight_score(s, background) > 0)
        return out

    def brief(self, ctx: GenContext) -> ContentBrief:
        picks = self._best_per_chapter(ctx)
        takeaways = [Takeaway(text=_clean(s.text), segment_ids=[s.id]) for _c, s in picks[:5]]
        return ContentBrief(
            webinar_topic=ctx.title,
            target_audience=AUDIENCE[ctx.config.audience],
            primary_message=takeaways[0].text if takeaways else ctx.title,
            key_takeaways=takeaways,
            tone=ctx.config.tone.replace("_", " "),
            call_to_action="Watch the full webinar recording.",
            suggested_title=ctx.title,
        )

    def blog(self, ctx: GenContext, brief: ContentBrief) -> BlogDraft:
        sections = []
        for c in ctx.chapters:
            if c.title in ("Introduction", "Closing Remarks", "Audience Q&A"):
                continue
            segs = [s for s in self._sentences(ctx, c) if len(s.text.split()) >= 6 and not _BACKREF.search(s.text)]
            paras = [
                Paragraph(text=" ".join(_clean(s.text) for s in segs[k : k + 3]), segment_ids=[s.id for s in segs[k : k + 3]])
                for k in range(0, min(len(segs), 9), 3)
            ]
            if paras:
                sections.append(BlogSection(heading=c.title, paragraphs=paras))
        intro_ids = [t.segment_ids[0] for t in brief.key_takeaways[:1]]
        return BlogDraft(
            title=brief.suggested_title,
            meta_description=(brief.primary_message[:157] + "...") if len(brief.primary_message) > 160 else brief.primary_message,
            keywords=top_keywords(ctx.transcript.segments, k=6),
            excerpt=brief.primary_message,
            introduction=[Paragraph(text=f"In our recent webinar, {ctx.title}, the speakers made one point again and again: {brief.primary_message}", segment_ids=intro_ids)],
            sections=sections,
            conclusion=[Paragraph(text=" ".join(t.text for t in brief.key_takeaways[:2]), segment_ids=[i for t in brief.key_takeaways[:2] for i in t.segment_ids])],
            key_takeaways=[t.text for t in brief.key_takeaways],
            internal_link_suggestions=[c.title for c in ctx.chapters[1:4]],
        )

    def linkedin(self, ctx: GenContext, brief: ContentBrief) -> LinkedInPosts:
        tags = ["#" + k.replace("-", "").capitalize() for k in top_keywords(ctx.transcript.segments, k=3)]
        posts = []
        for t in brief.key_takeaways[: ctx.config.linkedin_post_count]:
            seg = ctx.transcript.get(t.segment_ids[0])
            text = (
                f"{t.text}\n\nThat was one of the points {seg.speaker} made in our webinar, {ctx.title}.\n\n"
                f"What's your experience been? The full recording is linked in the comments."
            )
            posts.append(SocialPost(angle=t.text[:60], text=text, hashtags=tags, segment_ids=t.segment_ids))
        return LinkedInPosts(posts=posts)

    def quotes(self, ctx: GenContext, brief: ContentBrief) -> QuoteSet:
        background = self._background(ctx)
        ranked = sorted(
            (s for s in ctx.transcript.segments if 8 <= len(s.text.split()) <= 35),
            key=lambda s: self._insight_score(s, background), reverse=True,
        )
        verbatim = [VerbatimQuote(segment_id=s.id, quote=s.text, why="Strong standalone statement.")
                    for s in ranked[: ctx.config.quote_count] if self._insight_score(s, background) > 0]
        editorial = [EditorialTakeaway(text=t.text, segment_ids=t.segment_ids) for t in brief.key_takeaways[:3]]
        return QuoteSet(verbatim=verbatim, editorial=editorial)

    def visuals(self, ctx: GenContext, brief: ContentBrief, quotes: list[dict]) -> VisualBriefs:
        briefs = [
            VisualBrief(
                kind="quote_card", title=f"Quote card: {q['speaker']}", aspect_ratio="1:1",
                layout="Large quote centred, speaker name and webinar title beneath",
                visual_direction="Dark background, large high-contrast type, brand accent on the quotation marks.",
                quote=q["quote"], speaker=q["speaker"], segment_ids=q["segment_ids"],
            )
            for q in quotes[:3]
        ]
        slides = [Slide(headline=brief.suggested_title, body="Lessons from our webinar", visual="Title over brand gradient")]
        slides += [Slide(headline=f"{i}.", body=t.text, visual="Single icon illustrating the idea")
                   for i, t in enumerate(brief.key_takeaways, 1)]
        slides.append(Slide(headline="Watch the full webinar", body=brief.call_to_action, visual="Recording thumbnail"))
        briefs.append(VisualBrief(
            kind="carousel", title=f"Carousel: {brief.suggested_title}", aspect_ratio="4:5",
            layout="One idea per slide, headline top-left, body below", visual_direction="Consistent brand colours, generous whitespace.",
            slides=slides, segment_ids=[i for t in brief.key_takeaways for i in t.segment_ids],
        ))
        return VisualBriefs(briefs=briefs)

    def revise(self, ctx: GenContext, brief: ContentBrief | None, schema: type[T], previous: dict, feedback: str) -> T:
        # Offline mode cannot rewrite prose; it returns the draft unchanged so the workflow can be exercised.
        return schema.model_validate(previous)


def make_generator(mode: str, model: str, effort: str) -> Generator:
    """Generator for the configured mode ("claude" or "offline")."""
    if mode == "offline":
        return OfflineGenerator()
    return ClaudeGenerator(model=model, effort=effort)
