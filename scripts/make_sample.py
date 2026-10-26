"""Build a short sample webinar to try the Content Factory without a real recording.

    python scripts/make_sample.py samples/

Produces, in the output folder:
  sample_webinar.mp4       ~4 minutes, 1280x720: a slide per section, a speaker name tag,
                           and two synthetic voices (macOS `say`: Samantha and Daniel)
  sample_transcript.json   the transcript in MeetStream's segment-list format
                           (speaker, transcript, start_time, end_time, words[])

Because every sentence is synthesised separately and its duration measured, the transcript
timings are exact, which makes this a good fixture for checking clip boundaries and captions.
Word timings inside a sentence are spread evenly (good enough for captions).

The script content is written to exercise the pipeline: clear spoken transitions between
sections (for chapter detection), a few strong standalone lines (for clips and quotes), a
"not public yet" line (for the sensitive-content flag) and a Q&A section.

Requires macOS (`say`) and FFmpeg built with libfreetype (for the slide text).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

TITLE = "Building AI Agents That Can Join Meetings"
VOICES = {"Maya Chen": "Samantha", "Dev Patel": "Daniel"}

# The webinar: (slide title, [(speaker, [sentences...]), ...]). One slide per section.
SCRIPT = [
    ("Welcome", [
        ("Maya Chen", ["Welcome everyone, and thanks for joining us today.",
                       "My name is Maya, and I lead developer relations here.",
                       "I'm joined by Dev, who has spent the last year building meeting agents in production."]),
        ("Dev Patel", ["Thanks Maya. Today we are going to talk about agents that can actually sit in a meeting, listen, and do useful work afterwards.",
                       "We'll cover why this is hard, how the pieces fit together, and what breaks when you ship it."]),
    ]),
    ("Why meeting agents", [
        ("Maya Chen", ["Let's talk about why meeting agents matter in the first place.",
                       "Most of the important decisions in a company are made out loud, in meetings, and then they disappear.",
                       "The notes are partial, the action items are forgotten, and the context lives in someone's head."]),
        ("Dev Patel", ["The biggest mistake developers make when building AI agents is assuming that giving an LLM access to tools automatically makes it an agent.",
                       "Tool access is the easy part.",
                       "The hard part is knowing what is happening in the conversation, who said it, and when it is appropriate to act.",
                       "An agent that interrupts a meeting at the wrong moment gets removed from the call very quickly."]),
    ]),
    ("How the pieces fit", [
        ("Dev Patel", ["Moving on to how the system actually works.",
                       "First, a bot joins the call on Zoom, Google Meet or Microsoft Teams, just like a participant would.",
                       "It records audio, and optionally video, and produces a transcript with speaker labels and timestamps.",
                       "When the meeting ends, a webhook tells your backend that the recording and transcript are ready.",
                       "Your agent never has to deal with the meeting platforms directly, which is where most of the pain used to be."]),
        ("Maya Chen", ["And the timestamps matter more than people expect.",
                       "If every summary, clip and action item points back to the exact moment in the recording, people can check the work instead of trusting it blindly."]),
    ]),
    ("Building the agent", [
        ("Dev Patel", ["Let's look at building the agent itself.",
                       "We use LangChain for the reasoning loop, and we connect it to the meeting tools through an MCP server.",
                       "The agent can list recent meetings, fetch a transcript, and pull the recording, all as tools.",
                       "Here's the rule we learned the hard way: keep the agent's job small and specific.",
                       "An agent that writes follow-up emails is useful. An agent that tries to run your whole company from meeting transcripts is a demo."]),
        ("Maya Chen", ["This next part is not public yet, so please don't share these slides.",
                       "We are testing agents that speak in the meeting, and the early lesson is that latency changes everything."]),
    ]),
    ("Production lessons", [
        ("Dev Patel", ["Now let's talk about what happens in production.",
                       "A reliable agent is one that knows what to do when its tools fail.",
                       "Transcripts arrive late, recordings are still processing, and meeting links expire.",
                       "You should treat every external call as something that will eventually fail, and design the retry path before the happy path.",
                       "The second lesson is observability. If you cannot replay exactly what the agent saw, you cannot debug what it did."]),
        ("Maya Chen", ["And consent matters. Just because a meeting was recorded doesn't mean everyone agreed to have their words reused.",
                       "Build the approval step into the product, not into a policy document nobody reads."]),
    ]),
    ("Questions", [
        ("Maya Chen", ["Let's open it up for questions.",
                       "The first question is about cost: how do you keep long meetings affordable?"]),
        ("Dev Patel", ["Good question. Cache the transcript once and let every downstream step reuse it, instead of sending the whole meeting again for each task.",
                       "That single change cut our costs more than any model switch."]),
        ("Maya Chen", ["Another question asks whether you need video at all."]),
        ("Dev Patel", ["For most agents, no. Audio and a good transcript cover summaries and action items.",
                       "You need video when you want to turn the meeting into clips people will actually watch."]),
    ]),
    ("Thank you", [
        ("Maya Chen", ["That's all we have time for today.",
                       "Thank you all for joining, and we'll share the recording with everyone who registered."]),
    ]),
]

GAP = 0.45  # silence between sentences
TURN_GAP = 0.8  # extra silence when the speaker changes


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def duration(path: Path) -> float:
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).strip())


def main(out_dir: Path) -> None:
    """Synthesise each sentence, concatenate with silences, build the transcript, then render the video."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        pieces: list[Path] = []
        utterances, slides = [], []
        t = 0.0
        n = 0

        # Audio is assembled from many small WAV pieces; `t` tracks the running time so every
        # sentence's start/end in the transcript matches the final audio exactly.
        def silence(seconds: float) -> Path:
            nonlocal n
            n += 1
            p = tmp / f"sil_{n}.wav"
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", f"{seconds}", str(p)])
            return p

        for slide_title, turns in SCRIPT:
            slide_start = t
            for speaker, sentences in turns:
                turn_start, words, texts = t, [], []
                for sentence in sentences:
                    n += 1
                    aiff, wav = tmp / f"s_{n}.aiff", tmp / f"s_{n}.wav"
                    run(["say", "-v", VOICES[speaker], "-r", "185", "-o", str(aiff), sentence])
                    run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ar", "22050", "-ac", "1", str(wav)])
                    d = duration(wav)
                    tokens = sentence.split()
                    step = d / len(tokens)
                    for i, tok in enumerate(tokens):
                        words.append({"word": tok.strip(".,?!").lower(), "punctuated_word": tok,
                                      "start": round(t + i * step, 3), "end": round(t + (i + 1) * step, 3),
                                      "confidence": 0.97, "speaker": list(VOICES).index(speaker)})
                    pieces.append(wav)
                    texts.append(sentence)
                    t += d
                    pieces.append(silence(GAP))
                    t += GAP
                utterances.append({"speaker": speaker, "transcript": " ".join(texts), "start_time": round(turn_start, 3),
                                   "end_time": round(t - GAP, 3), "words": words})
                pieces.append(silence(TURN_GAP))
                t += TURN_GAP
            slides.append((slide_title, slide_start, t))

        concat = tmp / "list.txt"
        concat.write_text("".join(f"file '{p}'\n" for p in pieces))
        audio = tmp / "audio.wav"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat), str(audio)])
        total = duration(audio)

        # Slides: webinar title in the corner, current section large in the middle.
        filters = []
        for i, (name, a, b) in enumerate(slides):
            tf = tmp / f"slide_{i}.txt"
            tf.write_text(name)
            filters.append(f"drawtext=font=Helvetica:textfile='{tf}':fontsize=84:fontcolor=white:"
                           f"x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,{a:.2f},{b:.2f})'")
        hdr = tmp / "title.txt"
        hdr.write_text(TITLE)
        filters.append(f"drawtext=font=Helvetica:textfile='{hdr}':fontsize=32:fontcolor=0x93C5FD:x=60:y=50")
        for i, u in enumerate(utterances):
            sf = tmp / f"spk_{i}.txt"
            sf.write_text(u["speaker"])
            filters.append(f"drawtext=font=Helvetica:textfile='{sf}':fontsize=36:fontcolor=white:box=1:boxcolor=0x1F2937@0.85:"
                           f"boxborderw=14:x=60:y=h-120:enable='between(t,{u['start_time']:.2f},{u['end_time']:.2f})'")
        video = out_dir / "sample_webinar.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=0x0F172A:s=1280x720:r=25:d={total:.2f}",
             "-i", str(audio), "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(video)])

    (out_dir / "sample_transcript.json").write_text(json.dumps(utterances, indent=1))
    print(f"{video} ({total:.0f}s), {out_dir / 'sample_transcript.json'} ({len(utterances)} utterances)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "samples"))
