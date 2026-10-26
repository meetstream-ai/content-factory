"""Structured outputs the generators return, plus the per-job configuration.

These Pydantic models do double duty:
  * They are the JSON schemas Claude must answer in (structured outputs). Field
    `description`s are part of the prompt: the model reads them, so they say what a good
    value looks like.
  * They are the stored `content` of most asset types, and the schema edits are validated
    against (see EDITABLE_SCHEMAS in pipeline.py).

Every field that points at the recording is a transcript segment id, never a timestamp.
The pipeline resolves ids into SourceRefs and drops or flags anything that doesn't resolve.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class JobConfig(BaseModel):
    """Options chosen when a job is created. Stored on the job, so retries use the same settings."""

    audience: Literal["developers", "founders", "marketers", "general"] = "developers"
    tone: Literal["technical", "conversational", "educational", "thought_leadership", "company_blog"] = "technical"
    clip_count: int = Field(default=5, ge=1, le=12)
    blog_word_count: int = Field(default=1000, ge=300, le=3000)
    linkedin_post_count: int = Field(default=4, ge=1, le=10)
    quote_count: int = Field(default=8, ge=1, le=20)
    generate_visual_briefs: bool = True
    clip_formats: list[Literal["landscape", "portrait", "square"]] = Field(default_factory=lambda: ["landscape", "portrait"])
    burn_captions: bool = True
    min_clip_seconds: float = 15.0  # shorter clips rarely carry a complete thought
    max_clip_seconds: float = 75.0  # comfortably under platform limits (Shorts: 60 s is the sweet spot)


# ---------------------------------------------------------------- chapters


class ChapterName(BaseModel):
    index: int = Field(description="0-based index of the chapter in the list you were given")
    title: str = Field(description="Specific, 3-8 words, title case. Not 'Part 2'.")
    summary: str = Field(description="One or two sentences on what this chapter actually covers")
    keywords: list[str]


class ChapterNames(BaseModel):
    """Answer to the chapter-naming stage: one entry per chapter index it was given."""

    chapters: list[ChapterName]


# ---------------------------------------------------------------- clips


class ClipCandidate(BaseModel):
    start_segment_id: str = Field(description="First segment of the clip, e.g. s0042. Must open on a complete thought.")
    end_segment_id: str = Field(description="Last segment of the clip (inclusive). Must close the thought.")
    title: str
    hook_strength: int = Field(description="0-10: does the first sentence make a scroller stop?")
    standalone_clarity: int = Field(description="0-10: understandable with no other context from the webinar")
    educational_value: int = Field(description="0-10")
    audience_relevance: int = Field(description="0-10 for the stated target audience")
    engagement_potential: int = Field(description="0-10: surprising, contrarian, quotable or funny")
    reason: str = Field(description="One sentence a reviewer can check against the clip")
    platforms: list[Literal["linkedin", "youtube_shorts", "instagram_reels", "tiktok", "x"]]


class ClipCandidates(BaseModel):
    """Answer to the clip-selection stage. Candidates are re-checked and scored in validate.resolve_clip."""

    clips: list[ClipCandidate]


# ---------------------------------------------------------------- shared brief


class Takeaway(BaseModel):
    text: str
    segment_ids: list[str]


class ContentBrief(BaseModel):
    """The shared brief every downstream asset follows, so the campaign tells one story."""

    webinar_topic: str
    target_audience: str
    primary_message: str
    key_takeaways: list[Takeaway]
    tone: str
    call_to_action: str
    suggested_title: str


# ---------------------------------------------------------------- blog


class Paragraph(BaseModel):
    text: str
    segment_ids: list[str] = Field(description="Segments that support every claim in this paragraph")


class BlogSection(BaseModel):
    heading: str
    paragraphs: list[Paragraph]


class BlogDraft(BaseModel):
    """A blog article in structured form. Rendered to Markdown at export (export.blog_markdown)."""

    title: str
    meta_description: str = Field(description="At most 160 characters")
    keywords: list[str]
    excerpt: str
    introduction: list[Paragraph]
    sections: list[BlogSection]
    conclusion: list[Paragraph]
    key_takeaways: list[str]
    internal_link_suggestions: list[str] = Field(description="Topics a reader might want linked; no URLs")


# ---------------------------------------------------------------- social


class SocialPost(BaseModel):
    angle: str = Field(description="What this post is about, in a few words")
    text: str
    hashtags: list[str]
    segment_ids: list[str]


class LinkedInPosts(BaseModel):
    posts: list[SocialPost]


# ---------------------------------------------------------------- quotes


class VerbatimQuote(BaseModel):
    segment_id: str = Field(description="The segment the quote starts in")
    quote: str = Field(description="Exact words from the transcript. Trim filler at the edges only.")
    why: str


class EditorialTakeaway(BaseModel):
    text: str = Field(description="A rewritten line inspired by the webinar; never presented as a quote")
    segment_ids: list[str]


class QuoteSet(BaseModel):
    """Verbatim quotes (verified, attributed) and editorial takeaways (labelled AI-written, never attributed)."""

    verbatim: list[VerbatimQuote]
    editorial: list[EditorialTakeaway]


# ---------------------------------------------------------------- visuals


class Slide(BaseModel):
    headline: str
    body: str
    visual: str


class VisualBrief(BaseModel):
    """A design brief detailed enough for a designer (or a Canva template) to build without the webinar."""

    kind: Literal["quote_card", "carousel", "thumbnail"]
    title: str
    aspect_ratio: Literal["1:1", "4:5", "9:16", "16:9"]
    layout: str
    visual_direction: str
    quote: str | None = Field(default=None, description="For quote cards: copied exactly from the verbatim quotes given")
    speaker: str | None = None
    slides: list[Slide] = Field(default_factory=list)
    segment_ids: list[str]


class VisualBriefs(BaseModel):
    briefs: list[VisualBrief]
