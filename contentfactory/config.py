"""Runtime settings: environment variables, plus keys saved from the dashboard.

Every setting has a default, so `uvicorn contentfactory.api:app` works with no
configuration at all (offline mode, no MeetStream import). See docs/CONFIGURATION.md.

The OpenAI key, model and MeetStream key can also be set in the dashboard's Settings panel.
Those are saved to `<data_dir>/secrets.json` (readable only by the owner) and take precedence
over the environment, so they survive restarts. Settings is mutable for that reason: the API
and the pipeline share one instance, and a save takes effect for the next job.

| Variable              | Default                              | Meaning                                         |
|-----------------------|--------------------------------------|-------------------------------------------------|
| CF_DATA_DIR           | ./data                               | Database, recordings, renders, exports          |
| OPENAI_API_KEY        | (unset)                              | Enables generation with OpenAI (or set it in Settings) |
| CF_LLM                | openai if an OpenAI key is set, else offline | Which generator to use                  |
| CF_MODEL              | gpt-5-mini                           | OpenAI model id (a reasoning model)             |
| CF_EFFORT             | medium                               | Reasoning effort: low / medium / high / xhigh / max |
| CF_JOB_WORKERS        | 2                                    | Jobs processed at the same time                 |
| CF_WHISPER_MODEL      | small                                | Local speech-to-text model for recordings without a MeetStream transcript (tiny/base/small/medium) |
| MEETSTREAM_API_KEY    | (unset)                              | Enables MeetStream links, meeting links and bot ids |
| MEETSTREAM_BASE_URL   | https://api.meetstream.ai/api/v1     | Override for testing                            |
| CF_API_TOKEN          | (unset)                              | If set, the API requires this bearer token (read in api.py). While unset, recording links to non-public addresses are refused |
| CF_MAX_UPLOAD_MB      | 4096                                 | Largest recording file accepted by upload       |
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# Default OpenAI model. It must be a reasoning model: generators.py sends `reasoning.effort`.
DEFAULT_MODEL = "gpt-5-mini"

# Keys the dashboard can save, mapped to the Settings field each one sets.
SAVABLE = {"openai_api_key": "openai_api_key", "openai_model": "model", "meetstream_api_key": "meetstream_api_key"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CF_DATA_DIR", "data")).resolve())
    openai_api_key: str | None = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY"))
    # "openai" uses the OpenAI API; "offline" uses deterministic heuristics (tests, demos without a key).
    llm_mode: str = field(
        default_factory=lambda: os.environ.get("CF_LLM", "openai" if os.environ.get("OPENAI_API_KEY") else "offline")
    )
    model: str = field(default_factory=lambda: os.environ.get("CF_MODEL", DEFAULT_MODEL))
    effort: str = field(default_factory=lambda: os.environ.get("CF_EFFORT", "medium"))
    meetstream_api_key: str | None = field(default_factory=lambda: os.environ.get("MEETSTREAM_API_KEY"))
    meetstream_base_url: str = field(
        default_factory=lambda: os.environ.get("MEETSTREAM_BASE_URL", "https://api.meetstream.ai/api/v1")
    )
    job_workers: int = field(default_factory=lambda: int(os.environ.get("CF_JOB_WORKERS", "2")))
    whisper_model: str = field(default_factory=lambda: os.environ.get("CF_WHISPER_MODEL", "small"))
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("CF_MAX_UPLOAD_MB", "4096")))

    # On-disk layout under data_dir:
    #   contentfactory.db             SQLite: webinars, jobs, assets, versions, reviews, exports
    #   secrets.json                  keys saved from the dashboard (mode 600)
    #   uploads/<id>/                 dropped-in recordings, as received
    #   webinars/<webinar_id>/        recording.*, transcript.json, raw MeetStream responses
    #   jobs/<job_id>/clips|captions  rendered media for one job
    #   jobs/<job_id>/exports/        ZIP packages

    @property
    def db_path(self) -> Path:
        return self.data_dir / "contentfactory.db"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / "secrets.json"

    def apply_saved(self) -> None:
        """Overlay the keys saved from the dashboard onto this instance."""
        try:
            saved = json.loads(self.secrets_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for key, attr in SAVABLE.items():
            if saved.get(key):
                setattr(self, attr, saved[key])
        self._refresh_mode()

    def save(self, **values: str | None) -> None:
        """Save keys from the dashboard (None or "" removes one) and apply them now.

        Removing a saved key falls back to the environment variable, if there is one.
        """
        try:
            saved = json.loads(self.secrets_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
        env = {"openai_api_key": os.environ.get("OPENAI_API_KEY"), "openai_model": os.environ.get("CF_MODEL", DEFAULT_MODEL),
               "meetstream_api_key": os.environ.get("MEETSTREAM_API_KEY")}
        for key, value in values.items():
            value = (value or "").strip()
            if value:
                saved[key] = value
            else:
                saved.pop(key, None)
            setattr(self, SAVABLE[key], value or env[key])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Written owner-only from the start, so the keys are never briefly readable by others.
        fd = os.open(self.secrets_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(saved, f, indent=1)
        self._refresh_mode()

    def _refresh_mode(self) -> None:
        if "CF_LLM" not in os.environ:  # an explicit CF_LLM always wins
            self.llm_mode = "openai" if self.openai_api_key else "offline"

    def webinar_dir(self, webinar_id: str) -> Path:
        return self.data_dir / "webinars" / webinar_id

    def job_dir(self, job_id: str) -> Path:
        return self.data_dir / "jobs" / job_id


def load_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.apply_saved()
    return s
