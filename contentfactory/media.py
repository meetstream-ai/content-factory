"""Deterministic media work with FFmpeg: probing, audio extraction, clip cutting and caption burn-in.

No LLM is involved here. Everything shells out to `ffmpeg` / `ffprobe`, which must be on PATH.

Clip rendering in one picture:

    recording.mp4 --(-ss start -to end)--> cut --(filter graph)--> reframe to 1920x1080 / 1080x1920 / 1080x1080
                                                                  --(subtitles=clip.srt)--> burned-in captions
                                                                  --(libx264 + AAC, faststart)--> clip_01_portrait.mp4

When the source aspect ratio differs from the target (a 16:9 webinar going to 9:16), the
whole frame is kept and centred over a blurred, zoomed copy of itself, instead of cropping
to the middle. Webinars are mostly slides and screen shares, and a centre crop would cut
off the content people came for. Speaker-tracking crops are a later phase.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FORMATS = {
    "landscape": (1920, 1080),  # YouTube, LinkedIn feed
    "portrait": (1080, 1920),   # Shorts, Reels, TikTok
    "square": (1080, 1080),     # LinkedIn
}


class MediaError(RuntimeError):
    """An FFmpeg/ffprobe failure; the message ends with the last lines of FFmpeg's stderr."""


@dataclass
class MediaInfo:
    """What ffprobe tells us about a file. Width and height are None for audio-only files."""

    duration: float
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None


def require_ffmpeg() -> None:
    """Fail early with an install hint, rather than with a confusing FileNotFoundError mid-job."""
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise MediaError(f"{tool} is not installed (brew install ffmpeg / apt install ffmpeg).")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command; on failure raise MediaError with the tail of stderr (FFmpeg is verbose)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
        raise MediaError(f"{cmd[0]} failed: {tail}")
    return proc


def probe(path: Path) -> MediaInfo:
    """Duration and stream info for a media file."""
    out = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    # Cover art in an MP3/M4A shows up as a one-frame "video" stream; it is not real video.
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0)
    return MediaInfo(
        duration=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
    )


def extract_audio(src: Path, dest: Path) -> Path:
    """16 kHz mono WAV, the input speech-to-text expects."""
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dest)])
    return dest


def _video_filter(fmt: str, src_w: int, src_h: int) -> str:
    """FFmpeg filter graph that reframes the input to `fmt`; its output pad is labelled [v]."""
    w, h = FORMATS[fmt]
    if src_w * h == src_h * w:
        return f"[0:v]scale={w}:{h},setsar=1[v]"
    # Fit the whole frame (screen shares stay readable) over a blurred, cropped copy of itself.
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )


def _escape_filter_path(path: Path) -> str:
    """Escape a path for use inside a filter argument, where `:` and `'` are special."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def render_clip(src: Path, dest: Path, start: float, end: float, fmt: str, info: MediaInfo,
                burn_srt: Path | None = None) -> Path:
    """Cut [start, end] from `src` and render it at the target aspect ratio as H.264/AAC MP4.

    `start`/`end` are seconds in the recording. `burn_srt`, if given, must be timed relative to
    the clip (t=0 at `start`), which is what `captions.build_cues` produces: because `-ss` is
    placed before `-i` (input seeking), the cut's timestamps restart at zero, so the subtitles
    filter sees clip-relative time.
    """
    if not info.has_video:
        raise MediaError("Source has no video stream")
    if fmt not in FORMATS:
        raise MediaError(f"Unknown format {fmt!r}")
    graph = _video_filter(fmt, info.width or 1920, info.height or 1080)
    if burn_srt is not None:
        # libass sizes are relative to a 288-line canvas scaled to the output height, so the
        # portrait size is smaller in points but similar on screen relative to the frame width.
        size = 16 if fmt == "landscape" else 11
        style = f"FontName=Helvetica,FontSize={size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,MarginV=40"
        graph = graph.replace("[v]", "[vs]") + f";[vs]subtitles='{_escape_filter_path(burn_srt)}':force_style='{style}'[v]"
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
        "-filter_complex", graph, "-map", "[v]",
    ]
    if info.has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k"]
    # yuv420p + faststart: plays everywhere (browsers, LinkedIn, phones) and can start before fully downloaded.
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(dest)]
    _run(cmd)
    return dest


def render_audio_clip(src: Path, dest: Path, start: float, end: float) -> Path:
    """Audio-only recordings (MeetStream's audio-only bots) still get a playable excerpt."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
          "-vn", "-c:a", "aac", "-b:a", "160k", str(dest)])
    return dest


def validate_rendered(path: Path, expected_duration: float, tolerance: float = 1.5) -> list[str]:
    """Problems with a rendered file; an empty list means it is playable and the right length.

    This is the "are clips complete and playable?" check, done by re-probing the output
    instead of trusting FFmpeg's exit code.
    """
    if not path.exists() or path.stat().st_size == 0:
        return ["file missing or empty"]
    try:
        info = probe(path)
    except MediaError as e:
        return [f"unreadable: {e}"]
    problems = []
    if abs(info.duration - expected_duration) > tolerance:
        problems.append(f"duration {info.duration:.1f}s, expected {expected_duration:.1f}s")
    return problems
