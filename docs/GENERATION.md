# Generation with OpenAI

`contentfactory/generators.py` has two generators with the same interface:

| | `OpenAIGenerator` | `OfflineGenerator` |
|---|---|---|
| Used when | `OPENAI_API_KEY` is set (or `CF_LLM=openai`) | No key (or `CF_LLM=offline`) |
| Writes | Real copy: titles, articles, posts, briefs | Nothing new; picks and arranges the speakers' sentences |
| For | The product | Tests, demos, trying the workflow without a key |
| "Rewrite with AI" | Rewrites from feedback | Returns the draft unchanged (the dashboard hides the button) |

Whichever generator runs, **its output is checked the same way** (see [PIPELINE.md](PIPELINE.md)). Unknown citations are flagged, non-verbatim quotes are dropped, clip boundaries are recomputed, and scores are calculated in code.

## The interface

```python
class Generator(Protocol):
    name: str
    def name_chapters(ctx) -> ChapterNames
    def find_clips(ctx) -> ClipCandidates
    def brief(ctx) -> ContentBrief
    def blog(ctx, brief) -> BlogDraft
    def linkedin(ctx, brief) -> LinkedInPosts
    def quotes(ctx, brief) -> QuoteSet
    def visuals(ctx, brief, verified_quotes) -> VisualBriefs
    def revise(ctx, brief, schema, previous, feedback) -> schema
```

`ctx` is a `GenContext`: the webinar title, the transcript, the job's `JobConfig`, and the named chapters. The return types are the Pydantic models in `drafts.py`.

## How an OpenAI call is built

Every stage goes through `OpenAIGenerator._call`, which uses the Responses API:

```python
client.responses.with_raw_response.parse(
    model="gpt-5-mini",                           # CF_MODEL
    instructions=SYSTEM_PROMPT + "<transcript>…</transcript>",
    input="<stage instructions + chapter table + brief>",
    text_format=SchemaModel,
    reasoning={"effort": "medium"},               # CF_EFFORT
    max_output_tokens=16000,                      # 20000 for the blog and revisions
    prompt_cache_key="contentfactory-<webinar id>",
    store=False,
)
```

What each piece is for:

- **Structured outputs** (`text_format=<Pydantic model>`). The SDK sends the model as a strict JSON schema, so the response is guaranteed to match it. There's no JSON parsing or retry-on-bad-JSON code. The models' `Field(description=…)` texts are part of the prompt: the model reads them.
- **Prompt caching.** The instructions and transcript are identical for every stage of a job and come first in the request. OpenAI caches long identical prefixes automatically, so after the first call later stages read the transcript from the cache instead of paying full price for it again. `prompt_cache_key` (the webinar id) keeps a job's calls routed to the same cache. This matters: the transcript is by far the largest part of each request.
- **Reasoning effort.** `CF_EFFORT` sets how hard the model thinks: `medium` is the default; try `high` if blog quality matters more than cost. It covers reasoning tokens too, so `max_output_tokens` is set generously.
- **`store=False`.** Webinar transcripts aren't kept on OpenAI's side after the call.
- **Raw response first.** The code checks the raw response for `status: "incomplete"` (truncation) and `refusal` content *before* parsing. In both cases the text doesn't match the schema, and parsing first would hide the real reason behind a validation error.
- **Retries.** The SDK retries 429, 5xx and connection errors itself (`max_retries=3`). Whatever still fails becomes a `GenerationError` with a readable reason, and the job can be retried from that stage.

## The prompts

### System prompt

`SYSTEM_PROMPT` says the following once, with reasons, so the stage prompts can stay short:

- The transcript is the **only source of truth**. Each line is `[segment_id start-end] Speaker: text`.
- **Cite segment ids** for everything; an editor will click each citation to check it.
- **Don't invent** facts, numbers, names, product capabilities or opinions.
- **Verbatim quotes** must be exact; filler may be trimmed at the edges only.
- Write clean prose from messy speech, and never mention "the transcript".
- **Return fewer items** rather than weak ones.

The transcript as the model sees it:

```
[s0010 00:42-00:49] Dev Patel: The biggest mistake developers make when building AI agents is assuming that giving an LLM access to tools automatically makes it an agent.
[s0011 00:50-00:51] Dev Patel: Tool access is the easy part.
```

### Stage prompts

| Stage | Main instructions | Returns |
|---|---|---|
| `name_chapters` | Boundaries are fixed; give specific titles ("Why Tool Calling Isn't Enough", not "Discussion"); use "Introduction"/"Audience Q&A"/"Closing Remarks" only when true | `ChapterNames` |
| `find_clips` | Up to N clips within the length limits; open on the strongest line, not a back-reference; one complete idea; standalone; honest 0-10 sub-scores; no overlaps; spread across chapters | `ClipCandidates` |
| `brief` | One primary message, 3-5 takeaways with citations, a CTA pointing at the recording | `ContentBrief` |
| `blog` | About N words; organised around ideas, not the order they were said; intro + 3-6 H2 sections + conclusion; cite every paragraph; SEO fields | `BlogDraft` |
| `linkedin` | Up to N posts on different angles; strong first line; 80-220 words; 2-4 hashtags; ends with a question or pointer to the recording | `LinkedInPosts` |
| `quotes` | Up to N verbatim quotes (8-35 words, exact) + up to 5 editorial takeaways, which will be labelled as AI-written | `QuoteSet` |
| `visuals` | 3 quote cards using *verified* quotes, 1 carousel (5-8 slides), 1 thumbnail concept; concrete layout and visual direction | `VisualBriefs` |
| `revise` | The previous draft + the editor's feedback; apply it fully, keep the rest | same schema |

Audience and tone come from the job's configuration:

```
Target audience: software developers and AI engineers. Tone: technical.
```

### Changing a prompt

1. Edit the task string in the matching `OpenAIGenerator` method (or `SYSTEM_PROMPT`).
2. If the output needs a new field, add it to the model in `drafts.py`. Give it a `description`, since that's what the model sees.
3. If the field points at the recording, make it `segment_ids: list[str]` and resolve it in the pipeline with `validate.resolve_refs`, so it gets checked and shows up as timestamp chips.
4. Run `pytest tests/test_clients.py`. It checks the request shape (model, reasoning effort, strict structured output, caching, `store`) against a mocked API.

Keep `SYSTEM_PROMPT` identical across stages. Changing it per stage would break the cache.

## Cost and speed

Per job there are about 7 model calls: chapter names, clips, brief, then blog, social and quotes in parallel, then visual briefs. Each call sends the full transcript (cached after the first) plus a short task. Output is small, except for the blog.

To make it cheaper or faster:
- **Lower `CF_EFFORT`** (`low`) for short or simple webinars.
- **Fewer assets**: `clip_count`, `linkedin_post_count`, `quote_count`, `generate_visual_briefs: false`.
- **Choose a different model** with `CF_MODEL`, e.g. `gpt-5` (better writing) or `gpt-5-nano` (cheaper, for high volume). Use a reasoning model: every request sends `reasoning.effort`. Measure quality before switching: prompt changes and model changes interact.

The app doesn't track token usage yet. Each response's `usage` (including `input_tokens_details.cached_tokens`) is the place to start if you want per-job cost reporting.

## The offline generator

Its rules are deliberately simple, so it's obvious what it did:

| Method | How |
|---|---|
| `name_chapters` | "Introduction" / "Audience Q&A" / "Closing Remarks" when the chapter text says so; else the announced topic ("Now let's talk about **what happens in production**"); else the top distinctive keywords |
| `find_clips` | Try every sentence as an opener (skipping back-references); extend with the same speaker to about 2 × the minimum length; score hooks (`The biggest…`, `Never…`, numbers), clarity, insight words, topic overlap |
| `brief` | The most "insightful" sentence of each chapter becomes a takeaway (scored on insight words like *should, mistake, rule*, hooks and topic words, minus back-references) |
| `blog` | One section per content chapter, built from that chapter's cleaned-up sentences in groups of three |
| `linkedin` | A template around each takeaway, attributed to its speaker |
| `quotes` | The top-scoring 8-35-word sentences, as-is (so always verbatim) |
| `visuals` | Quote cards for the top quotes; a carousel from the takeaways |

It's good enough to exercise every button and check the plumbing. It is not what reviewers would approve for publication.
