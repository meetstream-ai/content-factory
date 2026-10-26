# Pipeline

A job runs ten stages. This page explains what each stage does, how its algorithm works, and what it does when something goes wrong. The code is in `contentfactory/pipeline.py`; each stage is a method named `_stage_<name>`.

| # | Stage | Uses the model? | Creates | Progress weight |
|---|---|---|---|---|
| 1 | `ingest` | no | webinar + `transcript.json` | 12 |
| 2 | `chapters` | names only | `chapters` | 10 |
| 3 | `content_brief` | yes | `content_brief` | 8 |
| 4 | `clip_selection` | proposes | `clip` × N | 10 |
| 5 | `blog` | yes (parallel) | `blog` | 12 |
| 6 | `social` | yes (parallel) | `linkedin_post` × N | 8 |
| 7 | `quotes` | yes (parallel) | `quote` × N, `takeaway` × N | 8 |
| 8 | `visual_briefs` | yes | `visual_brief` × N | 7 |
| 9 | `render_clips` | no | clip files | 20 |
| 10 | `quality_validation` | no | (moves everything to review) | 5 |

The weights only drive the progress bar.

---

## 1. Ingest

**Goal:** a recording on disk and a transcript in the IR (see [ARCHITECTURE.md](ARCHITECTURE.md#the-central-idea-timestamps-are-data)).

**MeetStream source** (`_ingest_meetstream`):

1. `GET /bots/{id}/detail` gives the platform and creation date. These make the default title, e.g. *"Zoom recording 2026-10-21"*, since bots have no meeting title.
2. `GET /bots/{id}/get_transcript` returns the speaker-labelled transcript.
3. `GET /bots/{id}/get_video` returns a presigned URL, which is downloaded straight away. If the bot has no video, it falls back to `GET /bots/{id}/get_audio`.
4. HTTP 202 means "still processing", and is retried every 15 s, up to 10 attempts.

The raw responses are saved next to the recording, for debugging. Details are in [MEETSTREAM.md](MEETSTREAM.md).

**Upload source** (`_ingest_upload`): the API has already saved the files; they're copied into the webinar folder.

**Both paths** then run `_finish_webinar`:

1. `ffprobe` the recording. If it has **no audio track**, the job fails: *"nothing to transcribe or clip"*.
2. Parse the transcript (`ingest.parse_transcript`). If there's no transcript, transcribe locally with faster-whisper (optional dependency).
3. Save `transcript.json` and record `has_video` and `duration_seconds`.

Then two job-level checks run on the transcript:

- **Low confidence:** mean word confidence below 75% gives a `low_confidence` warning.
- **Sensitive content:** a pattern match for *confidential, NDA, off the record, don't share, not public yet, unannounced, password, API key, salary*, plus emails and phone numbers. The matching segment ids are listed in a `sensitive_content` warning.

If there's no video, a `no_video` warning explains that clips will be audio excerpts.

### Transcript parsing

`ingest.py` accepts:

| Format | Detected by | Notes |
|---|---|---|
| MeetStream segment-list | JSON list with `transcript` / `start_time` | Word timings in `words[]` (`punctuated_word`, `start`, `end`, `confidence`) |
| MeetStream participant-keyed | JSON list with `participant.name` and `words[].start_timestamp.relative` | No utterance-level times; derived from the words |
| Wrapped JSON | object with `segments` / `transcript` / `utterances` / `results` list | |
| This app's own IR | object with `webinar_id` and `segments` | Segment ids are kept as they are |
| SRT / WebVTT | `.srt`/`.vtt` extension or a `WEBVTT` header | `Name: text` and `<v Name>` set the speaker |

**Sentence splitting** is what makes citations precise:

- **With word timings:** split after any word ending in `.`, `?` or `!`, so every segment's times are exact.
- **Without word timings:** split the text with a regex, and share the utterance's time span out in proportion to each sentence's character count. This is approximate.
- **Either way:** segments are capped at 25 s, so an unpunctuated monologue still yields citable pieces.

---

## 2. Chapters

Boundaries are found in code by `segmentation.detect_boundaries`. The model only names the chapters.

**Algorithm (TextTiling-style):**

1. **Blocks.** Cut the transcript into blocks of `clamp(duration/40, 20 s, 60 s)`, which gives about 40 blocks per recording.
2. **Vocabulary.** For each block, count its content words, with stopwords and spoken filler removed. Weight the counts by TF-IDF across blocks: words used everywhere (the webinar's own topic) count for little.
3. **Similarity curve.** At each gap between blocks, compute the cosine similarity of the 3 blocks before it and the 3 blocks after it.
4. **Depth.** For each gap: `(highest similarity to the left − this) + (highest to the right − this)`. Add **+0.25** if the next block opens with a spoken transition ("moving on to", "let's talk about", "let's open it up for questions", …). Add **+0.05** if the speaker changes.
5. **Select.** Take the deepest gaps first, up to `2 × √minutes` chapters (clamped to 3-9). Skip gaps with depth ≤ 0.05, and any gap closer than `max(30 s, duration / (2.5 × max_chapters))` to the start, the end or another chosen boundary.
6. **Snap.** Move each boundary up to 3 segments, onto a sentence that opens with a transition phrase. Failing that, onto a speaker change.

**Naming:** the generator receives the fixed segment ranges and returns a title, summary and keywords for each chapter. A chapter the model skips gets *"Chapter N"*.

**Output:** a single `chapters` asset. It also holds `youtube_chapters`, ready to paste into a YouTube description, starting at `00:00` as YouTube requires.

On the sample webinar, every detected boundary lands on a spoken transition. A test checks this (`test_chapter_boundaries_land_on_spoken_transitions`).

---

## 3. Content brief

One model call produces the **shared brief**: topic, audience, primary message, 3-5 key takeaways (each citing segments), tone, call to action and suggested title. The blog, posts, quotes and visual briefs all receive it, so the campaign tells one coherent story instead of each generator inventing its own angle.

The brief is itself a reviewable asset. It's exported as `webinar-summary.md`.

---

## 4. Clip selection

The generator proposes candidates. Each one has `start_segment_id`, `end_segment_id`, a title, five 0-10 sub-scores, a reason and target platforms. Then `validate.resolve_clip` makes each candidate exact:

| Step | Rule | QA note |
|---|---|---|
| 1 | Either segment id unknown → reject | `unknown_segment` (error) |
| 2 | First sentence refers back ("as I mentioned…", "and also…", "which…") → drop it, if the rest is still long enough | `boundary_adjusted` |
| 3 | A later sentence announces a new topic ("Moving on to…") → end before it | `topic_change` |
| 4 | Longer than `max_clip_seconds` → drop sentences from the end until it fits | `trimmed` |
| 5 | Shorter than `min_clip_seconds` → add the same speaker's next sentences, never a topic change | `extended` |
| 6 | Still under `min(min_clip_seconds, 8 s)` → reject | `too_short` |
| 7 | Pad 0.2 s before and 0.35 s after, but never into neighbouring speech or past the recording | |

Then the **score** is computed in code:

```
score = (hook × 25 + clarity × 25 + education × 20 + relevance × 15 + engagement × 10 + audio × 5) / 10     → 0-100
```

`audio` is 10 × the mean transcription confidence of the clip's segments (8 if unknown); it stands in for audio quality. A warning is added below 75% confidence, and an info note when more than one speaker is in the clip.

**Filtering** (`validate.dedupe_clips`):
- Drop clips scoring under **50**. Returning fewer suggestions beats padding with weak ones.
- Going from the best score down, drop any clip that overlaps a better one by more than 30% of the shorter clip.
- Keep at most `clip_count`, then order the survivors by time.

A `clips_filtered` info note on the job says how many candidates were proposed and how many were kept.

> The weights come from the product spec. They're starting heuristics, not a validated engagement model. Approval rates per sub-score are the data to tune them with.

---

## 5-7. Blog, social and quotes (run in parallel)

All three need only the transcript, chapters and brief, so they run concurrently. If one fails, the others keep their output and the retry re-runs only the failed one.

**Blog** (`BlogDraft`): intro, 3-6 sections, conclusion, key takeaways and SEO fields. Every paragraph carries `segment_ids`. QA adds:
- an `unsupported` **error** for paragraphs that cite nothing (check them for invented claims);
- `unknown_segment` warnings for bad ids;
- an info note with the word count.

**Social:** one `linkedin_post` asset per post: angle, text, hashtags and citations.

**Quotes** produce two kinds of output, kept strictly apart:

| | Verbatim quote (`quote`) | Editorial takeaway (`takeaway`) |
|---|---|---|
| Wording | The speaker's exact words | Rewritten by the model |
| Attributed to a speaker | Yes | Never |
| Verified | Must be found in the transcript, or it's dropped | Citations resolved |
| Label | Speaker + timestamp | *"AI-written takeaway (not a quote)"* |

**Quote verification** (`validate.locate_quote`):
1. Normalise both texts: lowercase, straight quotes, no punctuation, single spaces.
2. Look for the quote in up to 4 consecutive segments, starting at the cited segment and widening outwards. A slightly wrong citation is corrected rather than rejected.
3. Not found → the quote is **dropped**, and a `quotes_dropped` job warning lists examples. The words are not "fixed", since attributing invented words to a real person is the one failure this feature must not have.
4. Found → the speaker comes from the transcript, not the model. If the quote spans two speakers, it gets a `speaker_uncertain` warning.

---

## 8. Visual briefs

These need the **verified** quotes, so they run after stage 7. The generator gets the brief plus the verified quotes and returns:
- up to 3 quote cards (1:1);
- one LinkedIn carousel (4:5, 5-8 slides);
- one thumbnail concept (16:9).

If a quote card's text doesn't match a verified quote, or anything said in the webinar, it gets a `quote_not_verbatim` **error**.

These are design *briefs*: detailed enough to build in Canva or hand to a designer. Filling Canva templates automatically is a later phase.

---

## 9. Render clips

For each clip asset (`render_clip_files`):

1. **Captions.** Word timings come from the transcript, or are spread evenly across a segment if missing. They are grouped into cues of at most 7 words and 3.5 s, breaking at sentence ends and pauses over 0.8 s. Cues are shifted to clip time (t=0 at the clip start). Both `captions/clip_NN.srt` and `.vtt` are written.
2. **Video**, one MP4 per format in `clip_formats`:
   - `-ss start -to end` before `-i`. This makes the cut's timestamps start at 0, which is why clip-relative SRT works for burn-in.
   - **Reframe.** If the aspect ratio already matches, just scale. Otherwise centre the *whole* frame over a blurred, zoomed copy of itself. Slides and screen shares stay readable; a centre crop would cut them off.
   - **Burn in** the SRT with libass, if `burn_captions` is on. Font size is tuned per format.
   - Encode H.264 (CRF 21, `veryfast`) + AAC 160k, `yuv420p`, `+faststart`.
3. **Audio-only source:** write `clips/clip_NN_audio.m4a` instead.
4. **Verify.** Re-probe each output: it must exist, be readable, and be within 1.5 s of the expected length.
5. **Retry.** A failed render is tried up to 3 times. If it still fails, the clip keeps a `render_failed` **error** and stays in the queue. The job does not fail; the reviewer can adjust times, which re-renders.

| Format | Size | For |
|---|---|---|
| `landscape` | 1920×1080 | YouTube, LinkedIn feed |
| `portrait` | 1080×1920 | Shorts, Reels, TikTok |
| `square` | 1080×1080 | LinkedIn |

---

## 10. Quality validation

1. Every asset whose sources include a segment flagged as possibly sensitive gets a `sensitive_content` warning.
2. Every asset moves `GENERATED → PENDING_REVIEW`, logged as a `submit` by `system`.
3. The job becomes `awaiting_review`, with progress at 100.

---

## Failures and retries

- **Any stage raises:** the job becomes `failed`, with `error_message` set to `"<stage>: <reason>"` and `current_stage` set to the failed stage.
- **Error messages:** our own error types carry readable messages (MeetStream, transcript, FFmpeg, generation). Anything else is shown with its exception type, since it's a bug.
- **Retry:** `POST /jobs/{id}/retry`, or the button in the dashboard, re-queues the job:
  - stages already in `completed` are skipped, so nothing finished is regenerated or paid for again;
  - the failed stage first deletes any assets it created on the failed attempt. Those were never reviewed, because review only opens when the job finishes.
- **Ingest failure:** if `ingest` itself failed, a retry creates a new webinar record.

| Failure | Behaviour |
|---|---|
| MeetStream still processing (202) | Waits and retries up to ~2 minutes, then fails with "still processing"; retry later |
| No audio track | Fails: "nothing to transcribe or clip" |
| No transcript, faster-whisper not installed | Fails, explaining both fixes |
| No video | Continues: text assets, plus audio excerpts with captions |
| Model refusal / bad credentials / rate limit | Stage fails with that reason; retry resumes there |
| One clip fails to render | Clip keeps a `render_failed` error; job continues |
| Export fails (disk) | Recorded as a failed export; retry the export, nothing is regenerated |

## QA notes

| Code | Level | Where | Meaning |
|---|---|---|---|
| `unknown_segment` | warning/error | any | Cited ids that don't exist (an error for a clip, which is then rejected) |
| `unsupported` | error | blog, briefs, posts | Content that cites nothing from the webinar |
| `boundary_adjusted` | info | clip | Dropped an opening line that referred back |
| `topic_change` | info | clip | Ended before the speaker moved to a new topic |
| `trimmed` / `extended` | info | clip | Fitted to the length limits on sentence boundaries |
| `too_short` | error | clip | Rejected |
| `low_confidence` | warning | clip, job | Transcription confidence under 75% |
| `multi_speaker` | info | clip | More than one speaker |
| `speaker_uncertain` | warning | quote | Quote spans two speakers |
| `quote_not_verbatim` | error | visual brief | Quote card text isn't something anyone said |
| `sensitive_content` | warning | job, any | Possibly confidential or personal content |
| `render_failed` | error | clip | A format failed to render after retries |
| `no_recording` | warning | clip | Captions only |
| `no_video` | warning | job | Audio-only recording |
| `quotes_dropped` | warning | job | Proposed quotes that weren't verbatim |
| `clips_filtered` | info | job | Candidates proposed vs. kept |
| `word_count` | info | blog | Length of the draft |

Approving an asset with any **error** needs a written justification. It's stored with the approval and exported in the manifest.
