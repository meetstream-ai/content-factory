import json
import os
import subprocess
from pathlib import Path

import pytest

os.environ["CF_NO_APP"] = "1"  # don't build the module-level app against ./data during tests

from contentfactory.ingest import parse_transcript  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

# A tiny two-speaker webinar in MeetStream's segment-list format, with word timings.
SENTENCES = [
    ("Maya Chen", "Welcome everyone, and thanks for joining our session on meeting agents today."),
    ("Maya Chen", "I'm here with Dev, who builds these systems in production."),
    ("Dev Patel", "The biggest mistake developers make is assuming that tool access makes an LLM an agent."),
    ("Dev Patel", "Tool access is the easy part, and knowing when to act is the hard part."),
    ("Dev Patel", "An agent that interrupts at the wrong moment gets removed from the call quickly."),
    ("Maya Chen", "Moving on to production, what breaks first when you ship one of these?"),
    ("Dev Patel", "A reliable agent is one that knows what to do when its tools fail."),
    ("Dev Patel", "Transcripts arrive late and recordings are still processing, so design the retry path first."),
    ("Dev Patel", "This roadmap is confidential, so please don't share these slides outside the team."),
    ("Maya Chen", "That's all we have time for today, thank you for joining us."),
]
SENTENCE_SECONDS = 4.0


def make_meetstream_transcript() -> list[dict]:
    utterances, t = [], 0.0
    for speaker, text in SENTENCES:
        tokens = text.split()
        step = SENTENCE_SECONDS / len(tokens)
        words = [{"word": w.lower().strip(".,?"), "punctuated_word": w, "start": round(t + i * step, 3),
                  "end": round(t + (i + 1) * step, 3), "confidence": 0.95} for i, w in enumerate(tokens)]
        if utterances and utterances[-1]["speaker"] == speaker:
            u = utterances[-1]
            u["transcript"] += " " + text
            u["end_time"] = round(t + SENTENCE_SECONDS, 3)
            u["words"] += words
        else:
            utterances.append({"speaker": speaker, "transcript": text, "start_time": t,
                               "end_time": round(t + SENTENCE_SECONDS, 3), "words": words})
        t += SENTENCE_SECONDS + 0.5
    return utterances


@pytest.fixture
def ms_transcript_json() -> str:
    return json.dumps(make_meetstream_transcript())


@pytest.fixture
def transcript(ms_transcript_json):
    return parse_transcript("web_test", ms_transcript_json, "t.json")


@pytest.fixture(scope="session")
def tiny_video(tmp_path_factory) -> Path:
    """45 s of 640x360 video with a tone, enough for real FFmpeg cuts."""
    path = tmp_path_factory.mktemp("media") / "webinar.mp4"
    duration = len(SENTENCES) * (SENTENCE_SECONDS + 0.5)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=15:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)
    return path
