"""Job orchestration: ingest -> understand -> generate -> render -> validate -> review queue.

A *job* turns one source (a MeetStream bot or an upload) into a set of *assets* waiting
for review. It runs in a background thread pool, stage by stage:

    ingest            fetch/copy the recording, parse the transcript into the IR (ir.py)
    chapters          boundaries from segmentation.py, names from the generator
    content_brief     the shared story every later asset follows
    clip_selection    generator proposes moments; validate.py snaps, scores and filters them
    blog | social | quotes   run in parallel; all read the brief
    visual_briefs     needs the verified quotes, so runs after `quotes`
    render_clips      FFmpeg cuts, captions, per-file retries (media.py, captions.py)
    quality_validation   cross-checks, then every asset moves to PENDING_REVIEW

Each finished stage is recorded on the job (`completed`). If a stage fails, the job is
marked failed with the stage name; `retry()` resumes from there, so finished stages are not
regenerated (or paid for) again. A stage that re-runs first deletes the assets it created
on the failed attempt; those were never reviewed, because review only opens when the whole
job finishes.

This module also implements the reviewer actions (decide / edit / regenerate), because
edits to clips and quotes need the same transcript, rendering and validation machinery.
"""

from __future__ import annotations

import json
import logging
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import captions, media, review, validate
from .config import Settings
from .db import Store, now
from .drafts import BlogDraft, ContentBrief, JobConfig, SocialPost, VisualBrief
from .generators import Chapter, GenContext, Generator, GenerationError, make_generator
from .ingest import TranscriptError, parse_transcript, transcribe_media
from .ir import Transcript, fmt_clock, fmt_hms
from .meetstream import MeetStreamClient, MeetStreamError, with_retry_when_processing
from .segmentation import chapter_spans, detect_boundaries

log = logging.getLogger("contentfactory")

# (stage name, weight). Weights only drive the progress bar: roughly how long each stage takes.
STAGES: list[tuple[str, int]] = [
    ("ingest", 12),
    ("chapters", 10),
    ("content_brief", 8),
    ("clip_selection", 10),
    ("blog", 12),
    ("social", 8),
    ("quotes", 8),
    ("visual_briefs", 7),
    ("render_clips", 20),
    ("quality_validation", 5),
]
# Which asset types each stage creates, so a re-run can clear its own earlier output.
STAGE_ASSET_TYPES = {
    "chapters": ["chapters"],
    "content_brief": ["content_brief"],
    "clip_selection": ["clip"],
    "blog": ["blog"],
    "social": ["linkedin_post"],
    "quotes": ["quote", "takeaway"],
    "visual_briefs": ["visual_brief"],
}
# Independent of each other (they all read only the transcript, chapters and brief), so they
# run concurrently. With Claude, that turns three long calls into one wait.
PARALLEL_GROUP = ("blog", "social", "quotes")

# Which draft model an asset's content is, for edits and regeneration.
EDITABLE_SCHEMAS = {
    "blog": BlogDraft,
    "linkedin_post": SocialPost,
    "visual_brief": VisualBrief,
    "content_brief": ContentBrief,
    "takeaway": None,  # free text + citations: editable, but not regenerable
}
# Asset types "New variation" works for (the API hides `regenerate` for the others).
REGENERABLE = {kind for kind, schema in EDITABLE_SCHEMAS.items() if schema is not None}


class PipelineError(RuntimeError):
    """A job or request can't proceed; the message is shown to the user."""


@dataclass
class JobState:
    """Everything a stage needs, loaded once per run (the transcript is read from disk)."""

    job: dict
    webinar: dict
    transcript: Transcript
    config: JobConfig

    @property
    def job_id(self) -> str:
        return self.job["id"]


class ContentFactory:
    """The service object behind the API: owns the store, the worker pool and the generator.

    `generator_factory` and `meetstream_factory` are injection points for tests (a fake
    generator, a MeetStream client with a mock transport).
    """

    def __init__(self, settings: Settings, store: Store, generator_factory: Callable[[], Generator] | None = None,
                 meetstream_factory: Callable[[], MeetStreamClient] | None = None):
        self.settings = settings
        self.store = store
        self._generator_factory = generator_factory or (
            lambda: make_generator(settings.llm_mode, settings.model, settings.effort))
        self._meetstream_factory = meetstream_factory or (
            lambda: MeetStreamClient(settings.meetstream_api_key, settings.meetstream_base_url))
        self._pool = ThreadPoolExecutor(max_workers=settings.job_workers, thread_name_prefix="job")
        self._generator: Generator | None = None

    @property
    def generator(self) -> Generator:
        # Created lazily so constructing the app never needs Anthropic credentials.
        if self._generator is None:
            self._generator = self._generator_factory()
        return self._generator

    def meetstream(self) -> MeetStreamClient:
        return self._meetstream_factory()

    # ================================================================ jobs

    def submit(self, source: dict, config: JobConfig) -> dict:
        """Create a job and queue it. Returns immediately; poll the job for progress."""
        job = self.store.create_job(source=source, configuration=config.model_dump())
        self._pool.submit(self.run_job, job["id"])
        return job

    def retry(self, job_id: str) -> dict:
        """Re-queue a failed job. It resumes at the failed stage; finished stages are kept."""
        job = self.store.get_job(job_id)
        if job is None:
            raise PipelineError("Job not found")
        if job["status"] != "failed":
            raise PipelineError(f"Only failed jobs can be retried (this one is {job['status']}).")
        self.store.update_job(job_id, status="queued", error_message=None)
        self._pool.submit(self.run_job, job_id)
        return self.store.get_job(job_id)

    def run_job(self, job_id: str) -> None:
        """Run (or resume) every stage of a job. Never raises: failures are recorded on the job."""
        job = self.store.get_job(job_id)
        config = JobConfig.model_validate(job["configuration"])
        completed: list[str] = list(job["completed"])
        self.store.update_job(job_id, status="processing", generator=self.generator.name)
        stage = "ingest"  # name of the stage in progress, for the error message
        try:
            if "ingest" not in completed:
                self._stage(job_id, "ingest", completed)
                self._ingest(job_id)
                self._done(job_id, "ingest", completed)
            state = self._load_state(job_id, config)

            sequential = [s for s, _ in STAGES if s not in ("ingest", *PARALLEL_GROUP)]
            for stage in sequential[:3]:  # chapters, content_brief, clip_selection
                if stage not in completed:
                    self._run_stage(state, stage, completed)

            pending = [s for s in PARALLEL_GROUP if s not in completed]
            if pending:
                stage = "+".join(pending)
                self._stage(job_id, pending[0], completed)  # the UI shows the first as "current"
                with ThreadPoolExecutor(max_workers=len(pending)) as pool:
                    futures = {s: pool.submit(self._run_stage_body, state, s) for s in pending}
                    errors = []
                    for s, f in futures.items():
                        try:
                            f.result()
                            self._done(job_id, s, completed)
                        except Exception as e:  # keep the stages that worked; retry re-runs only the failed ones
                            errors.append((s, e))
                    if errors:  # report the first failure; the others will re-run on retry too
                        stage, err = errors[0]
                        raise err

            for stage in sequential[3:]:  # visual_briefs, render_clips, quality_validation
                if stage not in completed:
                    self._run_stage(state, stage, completed)

            self.store.update_job(job_id, status="awaiting_review", progress=100, current_stage=None, completed_at=now())
        except Exception as e:
            log.error("job %s failed in %s: %s\n%s", job_id, stage, e, traceback.format_exc())
            # Our own error types carry user-facing messages; anything else is a bug, so keep its type.
            message = str(e) if isinstance(e, (PipelineError, GenerationError, TranscriptError, MeetStreamError, media.MediaError)) \
                else f"{type(e).__name__}: {e}"
            self.store.update_job(job_id, status="failed", error_message=f"{stage}: {message}", current_stage=stage)

    def _stage(self, job_id: str, stage: str, completed: list[str]) -> None:
        """Mark `stage` as the one in progress."""
        self.store.update_job(job_id, current_stage=stage, progress=self._progress(completed))

    def _done(self, job_id: str, stage: str, completed: list[str]) -> None:
        """Record `stage` as finished; this is what makes retries resume instead of restart."""
        if stage not in completed:
            completed.append(stage)
        self.store.update_job(job_id, completed=completed, progress=self._progress(completed))

    @staticmethod
    def _progress(completed: list[str]) -> int:
        total = sum(w for _, w in STAGES)
        return round(100 * sum(w for s, w in STAGES if s in completed) / total)

    def _run_stage(self, state: JobState, stage: str, completed: list[str]) -> None:
        self._stage(state.job_id, stage, completed)
        self._run_stage_body(state, stage)
        self._done(state.job_id, stage, completed)

    def _run_stage_body(self, state: JobState, stage: str) -> None:
        """Dispatch to `_stage_<name>`, after clearing anything a failed earlier attempt left behind."""
        for t in STAGE_ASSET_TYPES.get(stage, []):
            self.store.delete_assets(state.job_id, t)  # a re-run replaces its own unreviewed output
        getattr(self, f"_stage_{stage}")(state)

    def _warn(self, job_id: str, *notes: dict) -> None:
        """Append job-level warnings (shown at the top of the review page and in the manifest)."""
        with self.store.transaction():  # read-modify-write; parallel stages may warn at the same time
            job = self.store.get_job(job_id)
            self.store.update_job(job_id, warnings=[*job["warnings"], *notes])

    # ================================================================ ingest

    def _ingest(self, job_id: str) -> None:
        """Create the webinar record (recording + transcript on disk) and attach it to the job."""
        job = self.store.get_job(job_id)
        source = job["source"]
        if source["type"] == "meetstream_recording":
            webinar = self._ingest_meetstream(source)
        elif source["type"] == "upload":
            webinar = self._ingest_upload(source)
        else:
            raise PipelineError(f"Unsupported source type {source['type']!r}")
        self.store.update_job(job_id, webinar_id=webinar["id"])
        transcript = self._read_transcript(webinar)
        self._warn(job_id, *validate.recording_warnings(transcript))
        if not webinar["has_video"]:
            self._warn(job_id, validate.note("warning", "no_video",
                       "The recording has no video track, so clips are delivered as audio excerpts with captions."))

    def _finish_webinar(self, webinar: dict, recording: Path | None, transcript_raw: bytes | str | None,
                        transcript_name: str) -> dict:
        """Shared tail of both ingest paths: check the media, build the IR, save it, update the webinar.

        Prefers a supplied transcript; falls back to local speech-to-text only when there is none.
        """
        wdir = self.settings.webinar_dir(webinar["id"])
        info = None
        if recording is not None:
            media.require_ffmpeg()
            info = media.probe(recording)
            if not info.has_audio:
                raise PipelineError("The recording has no audio track, so there is nothing to transcribe or clip.")
        duration = info.duration if info else None
        if transcript_raw is not None:
            transcript = parse_transcript(webinar["id"], transcript_raw, transcript_name, duration)
        elif recording is not None:
            audio = media.extract_audio(recording, wdir / "audio_16k.wav")
            transcript = transcribe_media(webinar["id"], audio, duration)
        else:
            raise PipelineError("Neither a transcript nor a recording is available.")
        tpath = wdir / "transcript.json"
        tpath.write_text(transcript.model_dump_json(indent=1))
        self.store.update_webinar(
            webinar["id"], recording_path=str(recording) if recording else None,
            has_video=int(bool(info and info.has_video)), duration_seconds=transcript.duration,
            transcript_path=str(tpath))
        return self.store.get_webinar(webinar["id"])

    def _ingest_upload(self, source: dict) -> dict:
        """Upload path: the API already saved the files under uploads/; copy them into the webinar folder."""
        webinar = self.store.create_webinar(title=source.get("title") or "Untitled webinar", source_type="upload",
                                            source_ref=source.get("filename"))
        wdir = self.settings.webinar_dir(webinar["id"])
        wdir.mkdir(parents=True, exist_ok=True)
        recording = None
        if source.get("recording_path"):
            src = Path(source["recording_path"])
            recording = wdir / f"recording{src.suffix.lower()}"
            shutil.copyfile(src, recording)
        raw, name = None, ""
        if source.get("transcript_path"):
            tp = Path(source["transcript_path"])
            raw, name = tp.read_bytes(), tp.name
        return self._finish_webinar(webinar, recording, raw, name)

    def _ingest_meetstream(self, source: dict) -> dict:
        """MeetStream path: bot detail -> transcript -> video (or audio) -> IR.

        The title defaults to "<Platform> recording <date>" because MeetStream bots have no
        meeting title; the user can supply one when creating the job.
        """
        bot_id = source["bot_id"]
        client = self.meetstream()
        try:
            detail = client.get_bot_detail(bot_id)
            title = source.get("title") or (
                f"{detail.get('Platform') or 'Meeting'} recording {str(detail.get('CreatedAt') or '')[:10]}".strip())
            webinar = self.store.create_webinar(title=title, source_type="meetstream_recording", source_ref=bot_id)
            wdir = self.settings.webinar_dir(webinar["id"])
            wdir.mkdir(parents=True, exist_ok=True)
            # Keep MeetStream's raw responses next to the recording, for debugging and re-parsing.
            (wdir / "meetstream_bot.json").write_text(json.dumps(detail, indent=1))

            transcript = with_retry_when_processing(client.get_transcript, bot_id)
            (wdir / "meetstream_transcript.json").write_text(json.dumps(transcript))

            # Video first (needed for video clips). Audio-only bots, which MeetStream's MCP creates
            # by default, have no video; fall back to the mixed audio, which always exists.
            recording = None
            try:
                video = with_retry_when_processing(client.get_video, bot_id)
                if video.get("video_url"):
                    recording = client.download(video["video_url"], wdir / "recording.mp4")
            except MeetStreamError as e:
                log.info("bot %s: no video (%s); falling back to audio", bot_id, e)
            if recording is None:
                audio = with_retry_when_processing(client.get_audio, bot_id)
                if not audio.get("audio_url"):
                    raise PipelineError("MeetStream returned neither a video nor an audio recording for this bot.")
                recording = client.download(audio["audio_url"], wdir / "recording.wav")
            return self._finish_webinar(webinar, recording, json.dumps(transcript), "meetstream.json")
        finally:
            client.close()

    # ================================================================ state

    def _read_transcript(self, webinar: dict) -> Transcript:
        return Transcript.model_validate_json(Path(webinar["transcript_path"]).read_text())

    def _load_state(self, job_id: str, config: JobConfig) -> JobState:
        job = self.store.get_job(job_id)
        webinar = self.store.get_webinar(job["webinar_id"])
        return JobState(job=job, webinar=webinar, transcript=self._read_transcript(webinar), config=config)

    def _chapters(self, state: JobState) -> list[Chapter]:
        """Chapters as stored in the `chapters` asset, so a resumed job sees exactly what the earlier run produced."""
        assets = self.store.list_assets(state.job_id, "chapters")
        if not assets:
            return []
        return [Chapter(index=c["index"], first=state.transcript.index(c["first_segment_id"]),
                        last=state.transcript.index(c["last_segment_id"]), title=c["title"],
                        summary=c["summary"], keywords=c["keywords"]) for c in assets[0]["content"]["chapters"]]

    def _ctx(self, state: JobState) -> GenContext:
        """What every generator call receives: title, transcript, job config, named chapters."""
        return GenContext(title=state.webinar["title"], transcript=state.transcript, config=state.config,
                          chapters=self._chapters(state))

    def _brief(self, state: JobState) -> ContentBrief:
        assets = self.store.list_assets(state.job_id, "content_brief")
        if not assets:
            raise PipelineError("Content brief missing")
        return ContentBrief.model_validate(assets[0]["content"])

    # ================================================================ generation stages

    def _stage_chapters(self, state: JobState) -> None:
        """Deterministic boundaries + generated names -> one `chapters` asset (also YouTube chapter text)."""
        t = state.transcript
        spans = chapter_spans(t, detect_boundaries(t))
        ctx = GenContext(title=state.webinar["title"], transcript=t, config=state.config,
                         chapters=[Chapter(index=i, first=a, last=b) for i, (a, b) in enumerate(spans)])
        names = {c.index: c for c in self.generator.name_chapters(ctx).chapters}
        # A chapter the model skipped still exists; it just gets a placeholder title to edit.
        chapters, refs = [], []
        for c in ctx.chapters:
            n = names.get(c.index)
            first, last = t.segments[c.first], t.segments[c.last]
            chapters.append({
                "index": c.index, "title": n.title if n else f"Chapter {c.index + 1}",
                "summary": n.summary if n else "", "keywords": n.keywords if n else [],
                "start": first.start if c.index else 0.0, "end": last.end,  # YouTube requires 00:00 first
                "first_segment_id": first.id, "last_segment_id": last.id,
            })
            refs.append(t.ref([first.id, last.id]).model_dump())
        yt = "\n".join(f"{fmt_hms(c['start']) if c['start'] >= 3600 else fmt_clock(c['start'])} {c['title']}" for c in chapters)
        self.store.create_asset(state.job_id, "chapters", f"{len(chapters)} chapters",
                                {"chapters": chapters, "youtube_chapters": yt}, source_references=refs,
                                author=self.generator.name)

    def _stage_content_brief(self, state: JobState) -> None:
        """The shared brief: primary message, audience, takeaways (each with citations), CTA."""
        brief = self.generator.brief(self._ctx(state))
        refs, qa = [], []
        for i, tk in enumerate(brief.key_takeaways):
            r, n = validate.resolve_refs(state.transcript, tk.segment_ids, f"Takeaway {i + 1}")
            refs += r
            qa += n
        self.store.create_asset(state.job_id, "content_brief", "Content brief", brief.model_dump(),
                                source_references=refs, qa=qa, author=self.generator.name)

    def _stage_clip_selection(self, state: JobState) -> None:
        """Candidates from the generator -> exact, scored, non-overlapping clips (not rendered yet)."""
        cands = self.generator.find_clips(self._ctx(state)).clips
        resolved, rejected = [], []
        for c in cands:
            clip, notes = validate.resolve_clip(c, state.transcript, state.config)
            (resolved.append((clip, notes)) if clip else rejected.extend(notes))
        kept = validate.dedupe_clips(resolved, state.config.clip_count)
        if len(kept) < len(cands):
            self._warn(state.job_id, validate.note(
                "info", "clips_filtered",
                f"{len(cands)} clip candidates proposed, {len(kept)} kept after boundary, overlap and score checks."))
        for pos, (clip, notes) in enumerate(kept):
            ref = state.transcript.ref([clip["start_segment_id"], clip["end_segment_id"]]).model_dump()
            ref.update(start_seconds=clip["start"], end_seconds=clip["end"])
            self.store.create_asset(state.job_id, "clip", clip["title"], clip, position=pos,
                                    source_references=[ref], qa=notes, author=self.generator.name)

    def _stage_blog(self, state: JobState) -> None:
        """One blog draft; every paragraph's citations are resolved and unsupported ones flagged."""
        draft = self.generator.blog(self._ctx(state), self._brief(state))
        refs, qa = self._blog_refs(state.transcript, draft)
        self.store.create_asset(state.job_id, "blog", draft.title, draft.model_dump(), source_references=refs,
                                qa=qa, author=self.generator.name)

    @staticmethod
    def _blog_refs(transcript: Transcript, draft: BlogDraft) -> tuple[list[dict], list[dict]]:
        """Source refs for all paragraphs, plus QA notes: unknown ids, uncited paragraphs, word count."""
        refs, qa = [], []
        paras = [("Introduction", p) for p in draft.introduction]
        paras += [(s.heading, p) for s in draft.sections for p in s.paragraphs]
        paras += [("Conclusion", p) for p in draft.conclusion]
        unsupported = 0
        for i, (where, p) in enumerate(paras):
            r, n = validate.resolve_refs(transcript, p.segment_ids, f"Paragraph {i + 1} ({where})")
            refs += r
            unsupported += any(x["code"] == "unsupported" for x in n)
            qa += [x for x in n if x["code"] != "unsupported"]
        if unsupported:
            qa.append(validate.note("error", "unsupported",
                                    f"{unsupported} paragraph(s) cite no part of the webinar; check them for invented claims."))
        words = sum(len(p.text.split()) for _, p in paras)
        qa.append(validate.note("info", "word_count", f"{words} words"))
        return refs, qa

    def _stage_social(self, state: JobState) -> None:
        """One asset per LinkedIn post, so each can be approved or rejected on its own."""
        posts = self.generator.linkedin(self._ctx(state), self._brief(state)).posts
        for pos, post in enumerate(posts[: state.config.linkedin_post_count]):
            refs, qa = validate.resolve_refs(state.transcript, post.segment_ids, "Post")
            self.store.create_asset(state.job_id, "linkedin_post", post.angle, post.model_dump(), position=pos,
                                    source_references=refs, qa=qa, author=self.generator.name)

    def _stage_quotes(self, state: JobState) -> None:
        """Verbatim quotes (verified against the transcript) and editorial takeaways (labelled as AI-written).

        A proposed quote whose words can't be found in the transcript is dropped, not "fixed":
        attributing invented words to a real person is the failure this product must not have.
        Quotes are re-located in the transcript, so a wrong segment id from the model is corrected.
        """
        result = self.generator.quotes(self._ctx(state), self._brief(state))
        t = state.transcript
        seen, dropped, pos = set(), [], 0
        for q in result.verbatim:
            span = validate.locate_quote(t, q.quote, q.segment_id)
            key = validate.normalize_text(q.quote)
            if span is None:
                dropped.append(q.quote)
                continue
            if key in seen:
                continue
            seen.add(key)
            speakers = {s.speaker for s in span}
            confs = [s.confidence for s in span if s.confidence is not None]
            qa = []
            if len(speakers) > 1:
                qa.append(validate.note("warning", "speaker_uncertain", "Quote spans more than one speaker; check attribution."))
            content = {
                "kind": "verbatim", "quote": q.quote.strip(), "speaker": span[0].speaker if len(speakers) == 1 else None,
                "segment_ids": [s.id for s in span], "start": span[0].start, "end": span[-1].end,
                "timestamp": fmt_hms(span[0].start), "confidence": round(sum(confs) / len(confs), 3) if confs else None,
                "why": q.why,
            }
            self.store.create_asset(state.job_id, "quote", q.quote[:80], content, position=pos,
                                    source_references=[t.ref(content["segment_ids"]).model_dump()], qa=qa,
                                    author=self.generator.name)
            pos += 1
            if pos >= state.config.quote_count:
                break
        if dropped:
            self._warn(state.job_id, validate.note(
                "warning", "quotes_dropped",
                f"Dropped {len(dropped)} proposed quote(s) whose wording doesn't appear in the transcript.",
                examples=dropped[:3]))
        for i, tk in enumerate(result.editorial):
            refs, qa = validate.resolve_refs(t, tk.segment_ids, "Takeaway")
            content = {"kind": "editorial", "label": "AI-written takeaway (not a quote)", "text": tk.text,
                       "segment_ids": tk.segment_ids}
            self.store.create_asset(state.job_id, "takeaway", tk.text[:80], content, position=i,
                                    source_references=refs, qa=qa, author=self.generator.name)

    def _verified_quotes(self, job_id: str) -> list[dict]:
        """Quotes that survived verification; the only quotes a quote card may use."""
        return [a["content"] for a in self.store.list_assets(job_id, "quote")]

    def _stage_visual_briefs(self, state: JobState) -> None:
        """Canva-ready briefs: quote cards (from verified quotes), a carousel, a thumbnail concept."""
        if not state.config.generate_visual_briefs:
            return
        quotes = self._verified_quotes(state.job_id)
        briefs = self.generator.visuals(self._ctx(state), self._brief(state), quotes).briefs
        for pos, b in enumerate(briefs):
            refs, qa = self._visual_refs(state.transcript, b, quotes)
            self.store.create_asset(state.job_id, "visual_brief", b.title, b.model_dump(), position=pos,
                                    source_references=refs, qa=qa, author=self.generator.name)

    @staticmethod
    def _visual_refs(transcript: Transcript, b: VisualBrief, quotes: list[dict]) -> tuple[list[dict], list[dict]]:
        """Refs for a brief; a quote card whose quote isn't verbatim gets a QA error."""
        refs, qa = validate.resolve_refs(transcript, b.segment_ids, b.title)
        if b.kind == "quote_card":
            verified = {validate.normalize_text(q["quote"]) for q in quotes}
            if not b.quote or (validate.normalize_text(b.quote) not in verified
                               and validate.locate_quote(transcript, b.quote, None) is None):
                qa.append(validate.note("error", "quote_not_verbatim",
                                        "This quote card's text doesn't match anything said in the webinar."))
        return refs, qa

    # ================================================================ rendering

    def render_clip_files(self, state: JobState, clip: dict, position: int) -> tuple[dict, list[dict]]:
        """Caption files, plus one MP4 per requested format (or an audio excerpt for audio-only sources).

        Returns `(files, notes)`: `files` maps a kind ("srt", "vtt", "landscape", "portrait",
        "square", "audio") to a path relative to the job folder; `notes` holds QA errors for
        renders that failed. A failed render doesn't fail the job: the clip stays in the
        queue with the error, and the reviewer can adjust and re-render it by editing.
        """
        jdir = self.settings.job_dir(state.job_id)
        stem = f"clip_{position + 1:02d}"
        segs = [s for s in state.transcript.segments if s.end > clip["start"] and s.start < clip["end"]]
        cues = captions.build_cues(segs, clip["start"], clip["end"])
        files: dict[str, str] = {}
        (jdir / "captions").mkdir(parents=True, exist_ok=True)
        srt = jdir / "captions" / f"{stem}.srt"
        srt.write_text(captions.to_srt(cues))
        (jdir / "captions" / f"{stem}.vtt").write_text(captions.to_vtt(cues))
        files["srt"] = f"captions/{stem}.srt"
        files["vtt"] = f"captions/{stem}.vtt"

        notes: list[dict] = []
        src = state.webinar.get("recording_path")
        if not src:
            notes.append(validate.note("warning", "no_recording", "No recording available: captions only."))
            return files, notes
        info = media.probe(Path(src))
        outputs = []
        if info.has_video:
            for fmt in state.config.clip_formats:
                out = jdir / "clips" / f"{stem}_{fmt}.mp4"
                outputs.append((fmt, out, lambda o=out, f=fmt: media.render_clip(
                    Path(src), o, clip["start"], clip["end"], f, info,
                    burn_srt=srt if state.config.burn_captions else None)))
        else:
            out = jdir / "clips" / f"{stem}_audio.m4a"
            outputs.append(("audio", out, lambda o=out: media.render_audio_clip(Path(src), o, clip["start"], clip["end"])))

        for fmt, out, render in outputs:
            last_error = None
            for _attempt in range(3):  # a failed render retries this one file, not the whole job
                try:
                    render()
                    problems = media.validate_rendered(out, clip["end"] - clip["start"])
                    if not problems:
                        last_error = None
                        break
                    last_error = "; ".join(problems)
                except media.MediaError as e:
                    last_error = str(e)
            if last_error:
                notes.append(validate.note("error", "render_failed", f"{fmt} render failed: {last_error}"))
            else:
                files[fmt] = str(out.relative_to(jdir))
        return files, notes

    def _stage_render_clips(self, state: JobState) -> None:
        """Render every selected clip. Clips render one at a time to keep CPU use predictable."""
        for asset in self.store.list_assets(state.job_id, "clip"):
            files, notes = self.render_clip_files(state, asset["content"], asset["position"])
            qa = [n for n in asset["qa"] if n["code"] not in ("render_failed", "no_recording")] + notes
            self.store.update_asset_fields(asset["id"], files=files, qa=qa)

    def _stage_quality_validation(self, state: JobState) -> None:
        """Last cross-checks, then open review: every asset moves GENERATED -> PENDING_REVIEW."""
        warnings = self.store.get_job(state.job_id)["warnings"]
        for asset in self.store.list_assets(state.job_id):
            qa = asset["qa"] + validate.sensitive_overlap(asset["source_references"], warnings)
            self.store.update_asset_fields(asset["id"], qa=qa)
            if asset["status"] == review.GENERATED:
                self._transition(asset, "submit", "system")

    # ================================================================ review actions

    def _transition(self, asset: dict, decision: str, reviewer: str, feedback: str | None = None) -> dict:
        """Apply a review decision (validated by review.py) and log it in asset_reviews."""
        to_status = review.next_status(decision, asset["status"])
        self.store.set_asset_status(asset["id"], to_status)
        self.store.add_review(asset["id"], reviewer, decision, asset["status"], to_status, asset["version"], feedback)
        return self.store.get_asset(asset["id"])

    def _job_ready_for_review(self, asset: dict) -> None:
        """Review opens only when the whole job has finished, so stages never race with reviewers."""
        job = self.store.get_job(asset["job_id"])
        if job["status"] != "awaiting_review":
            raise review.ReviewError("This job is still processing; review opens when it finishes.")

    def decide(self, asset_id: str, decision: str, reviewer: str, feedback: str | None = None) -> dict:
        """approve / reject / request_changes / reopen.

        Two rules on top of the state machine: requesting changes needs feedback (otherwise
        nobody knows what to change), and approving an asset with QA errors needs a note
        explaining why it's fine (it is recorded in the export manifest).
        """
        asset = self._asset(asset_id)
        self._job_ready_for_review(asset)
        if decision == "request_changes" and not (feedback or "").strip():
            raise review.ReviewError("Say what should change when requesting changes.")
        if decision == "approve" and any(n["level"] == "error" for n in asset["qa"]):
            if not (feedback or "").strip():
                raise review.ReviewError("This asset has QA errors. Add a note explaining why it is fine to approve it.")
        return self._transition(asset, decision, reviewer, feedback)

    def _asset(self, asset_id: str) -> dict:
        asset = self.store.get_asset(asset_id)
        if asset is None:
            raise PipelineError("Asset not found")
        return asset

    def _state_for_asset(self, asset: dict) -> JobState:
        job = self.store.get_job(asset["job_id"])
        return self._load_state(job["id"], JobConfig.model_validate(job["configuration"]))

    def edit(self, asset_id: str, reviewer: str, title: str | None, content: dict[str, Any] | None) -> dict:
        """Save an editor's change as a new version and put it back in the queue.

        `content` is merged over the current content, so callers send only the fields they
        changed. Type-specific rules:
          * clip: new `start`/`end` seconds; the clip and its captions are re-rendered.
          * quote: the text must still be found in the transcript (else ReviewError).
          * takeaway: free text; citations are re-resolved.
          * chapters: boundaries must reference real segments; YouTube text is rebuilt.
          * blog / linkedin_post / visual_brief / content_brief: validated against the draft schema.
        The asset goes edit -> REVISED -> PENDING_REVIEW: an edit is never self-approving.
        """
        asset = self._asset(asset_id)
        self._job_ready_for_review(asset)
        review.next_status("edit", asset["status"])  # fail fast before doing work
        state = self._state_for_asset(asset)
        t = state.transcript
        new_title = title or asset["title"]
        files, refs, qa = asset["files"], asset["source_references"], asset["qa"]
        merged = {**asset["content"], **(content or {})}
        kind = asset["asset_type"]

        if kind == "clip":
            start = float(merged["start"])
            end = float(merged["end"])
            if not (0 <= start < end <= t.duration + 0.5) or end - start < 3:
                raise review.ReviewError(f"Clip times must be within 0-{t.duration:.1f}s and at least 3 seconds long.")
            segs = [s for s in t.segments if s.end > start and s.start < end]
            if not segs:
                raise review.ReviewError("No speech in that time range.")
            merged.update(start=round(start, 3), end=round(end, 3), duration=round(end - start, 3),
                          start_segment_id=segs[0].id, end_segment_id=segs[-1].id,
                          transcript_text=" ".join(s.text for s in segs))
            merged["title"] = new_title
            files, notes = self.render_clip_files(state, merged, asset["position"])
            ref = t.ref([segs[0].id, segs[-1].id]).model_dump()
            ref.update(start_seconds=start, end_seconds=end)
            refs, qa = [ref], notes
        elif kind == "quote":
            span = validate.locate_quote(t, merged["quote"], (merged.get("segment_ids") or [None])[0])
            if span is None:
                raise review.ReviewError("A verbatim quote must match the speaker's words. "
                                         "Rewritten lines belong in a takeaway, which isn't attributed to anyone.")
            merged.update(segment_ids=[s.id for s in span], start=span[0].start, end=span[-1].end,
                          timestamp=fmt_hms(span[0].start))
            refs, qa = [t.ref(merged["segment_ids"]).model_dump()], []
        elif kind == "takeaway":
            refs, qa = validate.resolve_refs(t, merged.get("segment_ids", []), "Takeaway")
        elif kind == "chapters":
            for c in merged["chapters"]:
                if not (t.has(c["first_segment_id"]) and t.has(c["last_segment_id"])):
                    raise review.ReviewError("Chapter boundaries must reference transcript segments.")
            merged["youtube_chapters"] = "\n".join(f"{fmt_clock(c['start'])} {c['title']}" for c in merged["chapters"])
        else:
            schema = EDITABLE_SCHEMAS.get(kind)
            if schema is None:
                raise review.ReviewError(f"{kind} assets can't be edited.")
            draft = schema.model_validate(merged)
            merged = draft.model_dump()
            refs, qa = self._refs_for(kind, t, draft, state)

        before = asset["status"]
        asset = self.store.new_version(asset_id, title=new_title, content=merged, files=files, source_references=refs,
                                       qa=qa, status=review.next_status("edit", before), author=reviewer)
        self.store.add_review(asset_id, reviewer, "edit", before, asset["status"], asset["version"])
        return self._transition(asset, "submit", "system")

    def _refs_for(self, kind: str, t: Transcript, draft, state: JobState) -> tuple[list[dict], list[dict]]:
        """Re-derive source refs and QA notes for an edited or regenerated draft."""
        if kind == "blog":
            return self._blog_refs(t, draft)
        if kind == "visual_brief":
            return self._visual_refs(t, draft, self._verified_quotes(state.job_id))
        if kind == "content_brief":
            refs, qa = [], []
            for i, tk in enumerate(draft.key_takeaways):
                r, n = validate.resolve_refs(t, tk.segment_ids, f"Takeaway {i + 1}")
                refs, qa = refs + r, qa + n
            return refs, qa
        return validate.resolve_refs(t, getattr(draft, "segment_ids", []), kind)

    def regenerate(self, asset_id: str, reviewer: str, feedback: str) -> dict:
        """Ask the generator for a new variation of one asset, guided by the reviewer's feedback.

        Only that asset is regenerated; the rest of the package is untouched. The previous
        version stays in asset_versions. Offline mode returns the draft unchanged.
        """
        asset = self._asset(asset_id)
        self._job_ready_for_review(asset)
        review.next_status("regenerate", asset["status"])
        kind = asset["asset_type"]
        schema = EDITABLE_SCHEMAS.get(kind)
        if schema is None:
            raise review.ReviewError(f"{kind} assets can't be regenerated; edit them instead.")
        if not feedback.strip():
            raise review.ReviewError("Describe what the new version should do differently.")
        state = self._state_for_asset(asset)
        brief = None if kind == "content_brief" else self._brief(state)
        draft = self.generator.revise(self._ctx(state), brief, schema, asset["content"], feedback)
        refs, qa = self._refs_for(kind, state.transcript, draft, state)
        title = getattr(draft, "title", None) or getattr(draft, "angle", None) or asset["title"]
        asset_after = self.store.new_version(asset_id, title=title, content=draft.model_dump(), files=asset["files"],
                                             source_references=refs, qa=qa,
                                             status=review.next_status("regenerate", asset["status"]),
                                             author=self.generator.name)
        self.store.add_review(asset_id, reviewer, "regenerate", asset["status"], asset_after["status"],
                              asset_after["version"], feedback)
        return self._transition(asset_after, "submit", "system")

    def confirm_consent(self, job_id: str, reviewer: str, note: str | None) -> dict:
        """Record that the speakers agreed to reuse and the material is ours to use. Export requires it."""
        self.store.update_job(job_id, consent_confirmed=1, consent_note=f"{reviewer}: {note or 'confirmed'} ({now()})")
        return self.store.get_job(job_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
