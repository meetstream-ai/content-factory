"""SQLite persistence for webinars, jobs, assets, their versions, reviews and exports.

Data model (see SCHEMA below for every column):

    webinars 1--* content_jobs 1--* content_assets 1--* asset_versions   (every saved version)
                               |                   1--* asset_reviews    (every decision, with who/when/why)
                               1--* asset_exports                        (every ZIP built)

Design notes:
  * JSON columns (configuration, content, files, source_references, qa, ...) are stored as
    text and decoded automatically by `_row`; callers always see Python dicts/lists.
  * `content_assets` holds the current version; `asset_versions` holds all of them,
    including the current one, so history and "who changed what" survive edits.
  * One connection shared across threads, serialised by an RLock. SQLite with WAL is
    plenty for a single-server MVP; a hosted deployment would move to PostgreSQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS webinars (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,          -- meetstream_recording | upload
    source_ref TEXT,                    -- MeetStream bot id, or original filename
    recording_path TEXT,
    has_video INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    transcript_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_jobs (
    id TEXT PRIMARY KEY,
    webinar_id TEXT REFERENCES webinars(id),
    status TEXT NOT NULL,               -- queued | processing | awaiting_review | failed
    configuration TEXT NOT NULL,
    source TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    current_stage TEXT,
    completed TEXT NOT NULL DEFAULT '[]',
    warnings TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    generator TEXT,
    consent_confirmed INTEGER NOT NULL DEFAULT 0,
    consent_note TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS content_assets (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES content_jobs(id),
    asset_type TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '{}',
    source_references TEXT NOT NULL DEFAULT '[]',
    qa TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_job ON content_assets(job_id, asset_type, position);
CREATE TABLE IF NOT EXISTS asset_versions (
    asset_id TEXT NOT NULL REFERENCES content_assets(id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    files TEXT NOT NULL,
    source_references TEXT NOT NULL,
    author TEXT NOT NULL,               -- generator name, or the editor's id
    created_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, version)
);
CREATE TABLE IF NOT EXISTS asset_reviews (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES content_assets(id),
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL,             -- approve | reject | request_changes | edit | regenerate | reopen
    feedback TEXT,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    version INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_exports (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES content_jobs(id),
    destination TEXT NOT NULL,          -- zip
    path TEXT,
    asset_ids TEXT NOT NULL,
    export_status TEXT NOT NULL,
    error_message TEXT,
    exported_at TEXT NOT NULL
);
"""

# Columns stored as JSON text.
_JSON_FIELDS = {"configuration", "source", "completed", "warnings", "content", "files", "source_references", "qa", "asset_ids"}


def now() -> str:
    """UTC timestamp, ISO 8601 to the second. Sorts correctly as text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """Short random id with a readable prefix: job_..., ast_..., web_..., rev_..., exp_..."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k in _JSON_FIELDS & out.keys():
        if isinstance(out[k], str):
            out[k] = json.loads(out[k])
    return out


def _dump(v: Any) -> Any:
    return json.dumps(v) if isinstance(v, (dict, list)) else v


class Store:
    """All database access. Methods return plain dicts (JSON columns decoded) or lists of them."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, with explicit BEGIN/COMMIT in transaction().
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ generic

    def _insert(self, table: str, values: dict) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self._lock:
            self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", [_dump(v) for v in values.values()])

    def _update(self, table: str, key: str, key_value: str, values: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            self._conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", [*map(_dump, values.values()), key_value])

    def _one(self, sql: str, *args) -> dict | None:
        with self._lock:
            return _row(self._conn.execute(sql, args).fetchone())

    def _all(self, sql: str, *args) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, args).fetchall()]

    def transaction(self):
        """`with store.transaction():` runs the block atomically (and holds the lock throughout)."""
        store = self

        class _Tx:
            def __enter__(self):
                store._lock.acquire()
                store._conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
                store._lock.release()

        return _Tx()

    # ------------------------------------------------------------ webinars

    def create_webinar(self, **values) -> dict:
        values = {"id": new_id("web"), "created_at": now(), **values}
        self._insert("webinars", values)
        return self.get_webinar(values["id"])

    def get_webinar(self, webinar_id: str) -> dict | None:
        return self._one("SELECT * FROM webinars WHERE id = ?", webinar_id)

    def update_webinar(self, webinar_id: str, **values) -> None:
        self._update("webinars", "id", webinar_id, values)

    # ------------------------------------------------------------ jobs

    def create_job(self, source: dict, configuration: dict, webinar_id: str | None = None) -> dict:
        job_id = new_id("job")
        self._insert("content_jobs", {
            "id": job_id, "webinar_id": webinar_id, "status": "queued", "configuration": configuration,
            "source": source, "created_at": now(),
        })
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        return self._one("SELECT * FROM content_jobs WHERE id = ?", job_id)

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT j.*, w.title AS webinar_title, w.duration_seconds FROM content_jobs j "
            "LEFT JOIN webinars w ON w.id = j.webinar_id ORDER BY j.created_at DESC LIMIT ?", limit)

    def update_job(self, job_id: str, **values) -> None:
        self._update("content_jobs", "id", job_id, values)

    # ------------------------------------------------------------ assets

    def create_asset(self, job_id: str, asset_type: str, title: str, content: dict, *, position: int = 0,
                     files: dict | None = None, source_references: list | None = None, qa: list | None = None,
                     status: str = "GENERATED", author: str = "generator") -> dict:
        """Insert an asset and its version 1. `position` orders assets of the same type (clip 1, 2, ...)."""
        asset_id = new_id("ast")
        ts = now()
        values = {
            "id": asset_id, "job_id": job_id, "asset_type": asset_type, "position": position, "title": title,
            "content": content, "files": files or {}, "source_references": source_references or [],
            "qa": qa or [], "status": status, "version": 1, "created_at": ts, "updated_at": ts,
        }
        with self.transaction():
            self._insert("content_assets", values)
            self._snapshot(asset_id, 1, title, content, files or {}, source_references or [], author)
        return self.get_asset(asset_id)

    def _snapshot(self, asset_id, version, title, content, files, refs, author) -> None:
        self._insert("asset_versions", {
            "asset_id": asset_id, "version": version, "title": title, "content": content, "files": files,
            "source_references": refs, "author": author, "created_at": now(),
        })

    def get_asset(self, asset_id: str) -> dict | None:
        return self._one("SELECT * FROM content_assets WHERE id = ?", asset_id)

    def list_assets(self, job_id: str, asset_type: str | None = None) -> list[dict]:
        if asset_type:
            return self._all("SELECT * FROM content_assets WHERE job_id = ? AND asset_type = ? ORDER BY position", job_id, asset_type)
        return self._all("SELECT * FROM content_assets WHERE job_id = ? ORDER BY asset_type, position", job_id)

    def delete_assets(self, job_id: str, asset_type: str) -> None:
        """Only used when a stage re-runs after a failure, before anyone has reviewed its output."""
        with self.transaction():
            ids = [r["id"] for r in self._all("SELECT id FROM content_assets WHERE job_id = ? AND asset_type = ?", job_id, asset_type)]
            for i in ids:
                self._conn.execute("DELETE FROM asset_versions WHERE asset_id = ?", (i,))
                self._conn.execute("DELETE FROM asset_reviews WHERE asset_id = ?", (i,))
            self._conn.execute("DELETE FROM content_assets WHERE job_id = ? AND asset_type = ?", (job_id, asset_type))

    def new_version(self, asset_id: str, *, title: str, content: dict, files: dict, source_references: list,
                    qa: list, status: str, author: str) -> dict:
        """Replace an asset's current content and record it as version N+1 (atomically)."""
        with self.transaction():
            current = self.get_asset(asset_id)
            version = current["version"] + 1
            self._update("content_assets", "id", asset_id, {
                "title": title, "content": content, "files": files, "source_references": source_references,
                "qa": qa, "status": status, "version": version, "updated_at": now(),
            })
            self._snapshot(asset_id, version, title, content, files, source_references, author)
        return self.get_asset(asset_id)

    def set_asset_status(self, asset_id: str, status: str) -> None:
        self._update("content_assets", "id", asset_id, {"status": status, "updated_at": now()})

    def update_asset_fields(self, asset_id: str, **values) -> None:
        self._update("content_assets", "id", asset_id, {**values, "updated_at": now()})

    def list_versions(self, asset_id: str) -> list[dict]:
        return self._all("SELECT * FROM asset_versions WHERE asset_id = ? ORDER BY version", asset_id)

    # ------------------------------------------------------------ reviews & exports

    def add_review(self, asset_id: str, reviewer_id: str, decision: str, from_status: str, to_status: str,
                   version: int, feedback: str | None = None) -> None:
        self._insert("asset_reviews", {
            "id": new_id("rev"), "asset_id": asset_id, "reviewer_id": reviewer_id, "decision": decision,
            "feedback": feedback, "from_status": from_status, "to_status": to_status, "version": version,
            "reviewed_at": now(),
        })

    def list_reviews(self, asset_id: str) -> list[dict]:
        return self._all("SELECT * FROM asset_reviews WHERE asset_id = ? ORDER BY reviewed_at", asset_id)

    def add_export(self, job_id: str, destination: str, asset_ids: list[str], status: str,
                   path: str | None = None, error: str | None = None) -> dict:
        export_id = new_id("exp")
        self._insert("asset_exports", {
            "id": export_id, "job_id": job_id, "destination": destination, "path": path, "asset_ids": asset_ids,
            "export_status": status, "error_message": error, "exported_at": now(),
        })
        return self._one("SELECT * FROM asset_exports WHERE id = ?", export_id)

    def get_export(self, export_id: str) -> dict | None:
        return self._one("SELECT * FROM asset_exports WHERE id = ?", export_id)

    def list_exports(self, job_id: str) -> list[dict]:
        return self._all("SELECT * FROM asset_exports WHERE job_id = ? ORDER BY exported_at DESC", job_id)
