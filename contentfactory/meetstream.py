"""MeetStream REST client: find a bot's recording and pull its transcript and media.

In MeetStream, a "bot" is one meeting session: it joins a Zoom, Google Meet or Teams call,
records it, and (if configured) transcribes it. A bot id therefore identifies a recording.

This client covers the read-only operations that MeetStream's MCP server exposes as tools
(`list_bots`, `get_bot_detail`, `get_transcript`, `get_media_urls`). We call them over REST
rather than through an LLM + MCP so downloads and timestamps stay exact and testable.

API reference: https://docs.meetstream.ai/api-reference/introduction

Behaviour worth knowing:
  * Auth is `Authorization: Token <key>` (the literal word "Token", not "Bearer").
  * Media and transcript endpoints return HTTP 202 while the bot is still in the meeting or
    post-call processing is running. That is not an error; poll again (see
    `with_retry_when_processing`).
  * Media endpoints return *presigned S3 URLs* that expire after about an hour, so we
    download immediately instead of storing the URL.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx


class MeetStreamError(RuntimeError):
    """Any failure talking to MeetStream, with a message that says what to check.

    `status` is MeetStream's HTTP status code when the failure was an error response.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class MeetStreamNotReady(MeetStreamError):
    """The bot is still in the meeting or post-call processing is running (HTTP 202)."""


class RecordingLookupError(MeetStreamError):
    """A pasted link or id doesn't lead to a usable recording. The user's input to fix, not an outage."""


class MeetStreamClient:
    """Thin synchronous wrapper over the endpoints the Content Factory needs.

    `transport` exists for tests: pass an `httpx.MockTransport` to serve canned responses
    (it is used for both API calls and presigned downloads).
    """

    def __init__(self, api_key: str | None, base_url: str = "https://api.meetstream.ai/api/v1",
                 transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise MeetStreamError("MEETSTREAM_API_KEY is not set. Create a key at https://app.meetstream.ai/api-key.")
        self._transport = transport
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Token {api_key}"},
            timeout=httpx.Timeout(30.0, read=120.0),
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **params) -> Any:
        """GET and decode JSON, translating MeetStream's status codes into readable errors."""
        r = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        if r.status_code == 202:
            raise MeetStreamNotReady(f"{path}: still processing")
        if r.status_code == 403:
            raise MeetStreamError(f"{path}: forbidden (check MEETSTREAM_API_KEY, or the bot belongs to another account)", 403)
        if r.status_code == 404:
            raise MeetStreamError(f"{path}: not found", 404)
        if r.status_code == 409:
            # Documented for transcripts: transcription wasn't enabled on the bot, or it never got in.
            raise MeetStreamError(f"{path}: no transcript (transcription was not enabled, or the bot was never admitted)", 409)
        if r.status_code >= 400:
            raise MeetStreamError(f"{path}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.json()

    # ------------------------------------------------------------ bots

    def list_bots(self, status: str | None = None, platform: str | None = None,
                  date_from: str | None = None, date_to: str | None = None,
                  cursor: str | None = None) -> dict:
        """`GET /bots`: one page of bots, as `{"bots": [...], "hasNextPage": bool, "nextCursor": str}`.

        `platform` is one of `GMeet`, `Zoom`, `Teams` (case-sensitive). Dates are YYYY-MM-DD.
        The response documents `nextCursor`, but not the query parameter that takes it; we send
        it as `cursor`, which is unverified against the live API (the dashboard only reads page 1).
        """
        return self._get("/bots", status=status, platform=platform, cursor=cursor,
                         **{"from": date_from, "to": date_to})

    def get_bot_detail(self, bot_id: str) -> dict:
        """`GET /bots/{id}/detail`: platform, timings, status timeline, canonical `transcript_id`."""
        return self._get(f"/bots/{quote(bot_id, safe='')}/detail").get("bot_details", {})

    # ------------------------------------------------------------ transcripts

    def get_transcript(self, transcript_id: str) -> Any:
        """`GET /transcript/{transcript_id}/get_transcript`: the processed, speaker-labelled transcript.

        The body is a list of utterances in one of MeetStream's formats;
        `ingest.parse_transcript` understands all of them.
        """
        return self._get(f"/transcript/{quote(transcript_id, safe='')}/get_transcript")

    def get_bot_transcript(self, bot_id: str, detail: dict | None = None) -> Any:
        """The transcript of a bot: `transcript_id` from `GET /bots/{id}/detail`, then `get_transcript`.

        Pass `detail` when it was already fetched, to skip the second lookup.
        """
        if detail is None:
            detail = self.get_bot_detail(bot_id)
        transcript_id = detail.get("transcript_id")
        if not transcript_id:
            raise MeetStreamError(f"bot {bot_id}: no transcript (transcription was not enabled, or the bot was never admitted)", 409)
        return self.get_transcript(transcript_id)

    def list_transcriptions(self, bot_id: str) -> list[dict]:
        """`GET /bots/{id}/transcriptions`: every transcription run with provider and status."""
        return self._get(f"/bots/{quote(bot_id, safe='')}/transcriptions").get("transcriptions", [])

    # ------------------------------------------------------------ media

    def get_video(self, bot_id: str) -> dict:
        """`GET /bots/{id}/get_video`: `{"video_url": <presigned>, "video_info": {...}}`."""
        return self._get(f"/bots/{quote(bot_id, safe='')}/get_video")

    def get_audio(self, bot_id: str) -> dict:
        """`GET /bots/{id}/get_audio`: `{"audio_url": <presigned>}`. Always produced, even for video bots."""
        return self._get(f"/bots/{quote(bot_id, safe='')}/get_audio")

    def download(self, url: str, dest: Path) -> Path:
        """Stream a presigned S3 URL to disk (see `download_url`)."""
        return download_url(url, dest, self._transport)


def _private_hosts_blocked() -> bool:
    """Without CF_API_TOKEN the API is open, so pasted links must not reach this machine or its network."""
    return not os.environ.get("CF_API_TOKEN")


def _refuse_private_host(request: httpx.Request) -> None:
    """httpx request hook (it runs for every redirect too): refuse any address that isn't public internet.

    That covers loopback, private, link-local, carrier-grade NAT (100.64/10), reserved and
    IPv4-mapped IPv6 forms of all of them.

    Applies only while CF_API_TOKEN is unset. A name that doesn't resolve is let through; the request fails anyway.
    """
    if not _private_hosts_blocked():
        return
    host = request.url.host
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise RecordingLookupError(f"Links to private or local addresses ({host}) aren't fetched. "
                                       "Download the file and drop it in instead.")


def _url_client(transport: httpx.BaseTransport | None, timeout) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=True,
                        event_hooks={"request": [_refuse_private_host]})


def download_url(url: str, dest: Path, transport: httpx.BaseTransport | None = None) -> Path:
    """Stream a recording URL (e.g. a presigned S3 link) to disk.

    No auth header is sent: a presigned URL carries its own signature, and sending our API key
    to S3 would leak it. Writes to `<dest>.part` first so a failed download never leaves a
    truncated file at `dest`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _url_client(transport, httpx.Timeout(30.0, read=300.0)) as c:
        with c.stream("GET", url) as r:
            if r.status_code >= 400:
                raise MeetStreamError(f"Download failed: HTTP {r.status_code} (presigned links expire after ~1 hour)", r.status_code)
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
    tmp.replace(dest)
    return dest


def with_retry_when_processing(fn, *args, attempts: int = 10, wait_seconds: float = 15.0):
    """Call `fn(*args)`, waiting and retrying while MeetStream answers 202.

    MeetStream's docs suggest polling every 10-30 s. The defaults wait up to ~2.5 minutes,
    which covers post-call processing for typical meetings; after that the job fails with a
    clear message and can be retried from the dashboard.
    """
    for i in range(attempts):
        try:
            return fn(*args)
        except MeetStreamNotReady:
            if i == attempts - 1:
                raise
            time.sleep(wait_seconds)


# ---------------------------------------------------------------- resolving links to recordings
#
# People paste whatever link they have. We accept four kinds of reference:
#   1. a bare bot id                      "5b6c7d8e-1f2a-4b3c-9d4e-5f6a7b8c9d0e"
#   2. any MeetStream URL containing one  "https://app.meetstream.ai/.../5b6c7d8e-1f2a-..."
#   3. the meeting link itself            "https://zoom.us/j/123?pwd=..." / Meet / Teams
#   4. a direct link to the recording     MeetStream's S3 download link, or any .mp4/.mp3/... URL
# (4) is downloaded as is and needs no MeetStream key (see check_recording_url).
# (1) and (2) name the recording directly; we still check it with Get Bot Details. (3) is resolved by finding the bot MeetStream sent
# to that meeting, via List Bots, whose entries carry the meeting link.

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Bot statuses (lowercased, no spaces/underscores) while the meeting is still going on. Statuses
# from MeetStream's docs: Joining, InWaitingRoom, InMeeting, Recording, Stopped, NotAllowed, Denied, Done.
IN_PROGRESS_STATUSES = {"scheduled", "joining", "inwaitingroom", "inmeeting", "recording", "active"}
# Statuses where the bot never recorded anything.
NO_RECORDING_STATUSES = {"notallowed", "denied", "failed", "error", "cancelled", "canceled"}
# Anything else (Stopped, Done, or a status we don't know) is treated as "has a recording" and
# tried; if the media isn't ready, ingest waits on MeetStream's 202 responses as usual.

MAX_LIST_PAGES = 20  # List Bots pages to scan when matching a meeting link
# List Bots documents its entries only as "map from string to any"; Get Bot Details documents
# `MeetingLink`. For bots listed without a link, look it up, newest first, at most this many.
MAX_DETAIL_LOOKUPS = 50


@dataclass
class ResolvedRecording:
    bot_id: str
    resolved_from: str  # "bot_id" | "meetstream_url" | "meeting_link"
    bot: dict | None = None  # summarize_bot() of the match, for meeting links
    other_matches: list[dict] = field(default_factory=list)  # older bots for the same meeting


def normalize_meeting_link(link: str) -> str | None:
    """A comparable key for a Zoom / Google Meet / Teams link, or None if it isn't one.

    Ignores scheme, `www.`, case, trailing slashes and the query string (Zoom's `?pwd=` and
    tracking parameters vary between copies of the same invite), except for Teams, whose
    links are long opaque paths that we compare after URL-decoding.
    """
    try:
        u = urlsplit(link.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.")
    path = unquote(u.path).rstrip("/")
    if host == "meet.google.com":
        return f"meet:{path.lower().strip('/')}" if path.strip("/") else None
    if host == "zoom.us" or host.endswith(".zoom.us"):
        m = re.search(r"/(?:j|w|wc|s)/(\d+)", path)  # personal and webinar join paths
        return f"zoom:{m.group(1)}" if m else None
    if host.endswith("teams.microsoft.com") or host.endswith("teams.live.com"):
        return f"teams:{path.lower()}" if path else None
    return None


def classify_reference(ref: str) -> tuple[str, str]:
    """`(kind, value)`: kind is "bot_id", "meetstream_url" or "meeting_link".

    Raises RecordingLookupError for anything that is none of those (e.g. a YouTube link).
    """
    ref = ref.strip()
    if not ref:
        raise RecordingLookupError("Paste a MeetStream link, a meeting link, or a bot id.")
    if "://" not in ref and " " not in ref and "/" not in ref:
        return "bot_id", ref
    if is_recording_url(ref):
        return "recording_url", ref
    host = (urlsplit(ref).hostname or "").lower()
    if host == "meetstream.ai" or host.endswith(".meetstream.ai"):
        m = _UUID.search(ref)
        if not m:
            raise RecordingLookupError("That MeetStream link doesn't contain a bot id. Open the recording in the "
                                  "MeetStream dashboard and copy its link, or paste the meeting link instead.")
        return "meetstream_url", m.group(0)
    if normalize_meeting_link(ref):
        return "meeting_link", ref
    raise RecordingLookupError("Not a MeetStream link, a Zoom / Google Meet / Teams meeting link, a link to a recording "
                               "file, or a bot id.")


MEDIA_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".m4a", ".mp3", ".wav")
# MeetStream's recording download links are presigned S3 URLs: https://<bucket>.s3.amazonaws.com/<bot id>/meeting_recording.mp4?X-Amz-…
_MEETSTREAM_S3 = re.compile(r"^[a-z0-9][a-z0-9.-]*\.s3[\w.-]*\.amazonaws\.com$")


def is_recording_url(ref: str) -> bool:
    """Whether `ref` is a direct http(s) link to a media file (by path extension or an S3 host)."""
    u = urlsplit(ref.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    return bool(_MEETSTREAM_S3.match(u.hostname.lower())) or u.path.lower().endswith(MEDIA_EXTENSIONS)


def meetstream_bot_in_url(url: str) -> str | None:
    """The bot id in a MeetStream S3 recording link (its first path segment), else None."""
    u = urlsplit(url)
    if not _MEETSTREAM_S3.match((u.hostname or "").lower()):
        return None
    m = _UUID.search(u.path)
    return m.group(0) if m else None


def link_expires_at(url: str) -> datetime | None:
    """When a presigned S3 link stops working (X-Amz-Date + X-Amz-Expires), or None if it doesn't say."""
    q = {k.lower(): v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    try:
        signed = datetime.strptime(q["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return datetime.fromtimestamp(signed.timestamp() + int(q["x-amz-expires"]), timezone.utc)
    except (KeyError, ValueError):
        return None


def check_recording_url(url: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Confirm a recording link can be downloaded right now, before a job is created.

    Requests the first byte only. (A presigned GET URL can't be HEAD-requested: the signature
    covers the method.) Returns `{"bot_id", "expires_at", "size"}`; raises RecordingLookupError
    with the reason if the link has expired or doesn't point at a file.
    """
    expires = link_expires_at(url)
    if expires and expires <= datetime.now(timezone.utc):
        raise RecordingLookupError("That recording link has expired (MeetStream's download links last about an hour). "
                                   "Copy a fresh one from MeetStream, or download the file and drop it in.")
    try:
        with _url_client(transport, 20.0) as c:
            r = c.get(url, headers={"Range": "bytes=0-0"})
    except httpx.HTTPError as e:
        raise RecordingLookupError(f"Couldn't reach that recording link ({e.__class__.__name__}).") from e
    if r.status_code in (401, 403):
        raise RecordingLookupError("That recording link was refused. If it's a MeetStream download link it has probably "
                                   "expired: copy a fresh one, or download the file and drop it in.")
    if r.status_code >= 400:
        raise RecordingLookupError(f"That recording link didn't work (HTTP {r.status_code}).")
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        raise RecordingLookupError("That link opens a web page, not a recording file. Use the file's download link.")
    total = r.headers.get("content-range", "").rpartition("/")[2]
    return {"bot_id": meetstream_bot_in_url(url), "expires_at": expires.isoformat() if expires else None,
            "size": int(total) if total.isdigit() else None}


def _status(bot: dict) -> str:
    return str(bot.get("status") or "").lower().replace(" ", "").replace("_", "")


def find_bots_for_meeting(client: "MeetStreamClient", meeting_link: str) -> list[dict]:
    """Every bot on the account that joined `meeting_link`, newest first (as summarize_bot dicts)."""
    key = normalize_meeting_link(meeting_link)
    listed: list[dict] = []
    cursor, seen_cursors, seen_ids = None, set(), set()
    for _ in range(MAX_LIST_PAGES):
        page = client.list_bots(cursor=cursor)
        bots = [summarize_bot(b) for b in page.get("bots", [])]
        new = [b for b in bots if b["bot_id"] and b["bot_id"] not in seen_ids]
        seen_ids.update(b["bot_id"] for b in new)
        listed += new
        cursor = page.get("nextCursor")
        # Stop at the last page, or if the API ignores our cursor parameter (same page again).
        if not page.get("hasNextPage") or not cursor or cursor in seen_cursors or not new:
            break
        seen_cursors.add(cursor)

    # Fill in meeting links the list didn't include, from each bot's detail (newest bots first).
    missing = sorted((b for b in listed if not b["meeting_link"]),
                     key=lambda b: str(b.get("created_at") or ""), reverse=True)
    for b in missing[:MAX_DETAIL_LOOKUPS]:
        try:
            detail = summarize_bot(client.get_bot_detail(b["bot_id"]))
        except MeetStreamError:
            continue  # one unreadable bot shouldn't stop the search
        for k in ("meeting_link", "status", "created_at", "platform", "name"):
            b[k] = b[k] or detail[k]

    matches = [b for b in listed if b["meeting_link"] and normalize_meeting_link(b["meeting_link"]) == key]
    return sorted(matches, key=lambda b: str(b.get("created_at") or ""), reverse=True)


def resolve_recording(client: "MeetStreamClient", ref: str) -> ResolvedRecording:
    """Turn whatever the user pasted into the bot id whose recording we should process.

    Checked against MeetStream before any job exists, so a wrong id, another account's bot or
    a meeting that is still going on is reported straight away instead of as a failed job.
    """
    kind, value = classify_reference(ref)
    if kind != "meeting_link":
        try:
            detail = client.get_bot_detail(value)
        except MeetStreamError as e:
            if e.status == 404:
                raise RecordingLookupError(
                    f"MeetStream has no recording with the id {value} on this account. Check the id, and that "
                    "MEETSTREAM_API_KEY is a key for the MeetStream account that recorded it.") from e
            if e.status == 403:
                raise RecordingLookupError(
                    f"Recording {value} belongs to a different MeetStream account than MEETSTREAM_API_KEY.") from e
            raise
        bot = summarize_bot(detail)
        bot["bot_id"] = bot["bot_id"] or value
        _check_has_recording(bot)
        return ResolvedRecording(bot_id=value, resolved_from=kind, bot=bot)

    bots = find_bots_for_meeting(client, value)
    if not bots:
        raise RecordingLookupError("No MeetStream bot has joined that meeting link on this account. Send a bot to the "
                              "meeting first, or check you're using the same MeetStream API key.")
    finished = [b for b in bots if _status(b) not in IN_PROGRESS_STATUSES | NO_RECORDING_STATUSES]
    if finished:
        # A recurring meeting link can have many recordings; take the newest finished one.
        best = finished[0]
        return ResolvedRecording(bot_id=best["bot_id"], resolved_from="meeting_link", bot=best,
                                 other_matches=[b for b in bots if b is not best])
    _check_has_recording(bots[0])  # newest bot: still in the meeting, or never recorded it
    raise RecordingLookupError("MeetStream has no finished recording of that meeting yet.")


def _check_has_recording(bot: dict) -> None:
    """Raise RecordingLookupError if this bot is still in its meeting, or never recorded it."""
    if _status(bot) in IN_PROGRESS_STATUSES:
        raise RecordingLookupError(f"MeetStream's bot is still in that meeting (status: {bot['status']}). "
                                   "Try again after the meeting ends.")
    if _status(bot) in NO_RECORDING_STATUSES:
        raise RecordingLookupError(f"MeetStream's bot for that meeting didn't record it (status: {bot['status']}).")


def summarize_bot(bot: dict) -> dict:
    """Normalise one List Bots entry into the fields the UI shows.

    List Bots documents its items only as "map from string to any", and other endpoints use
    PascalCase (`BotID`, `CreatedAt`), so each field accepts several spellings.
    """
    def pick(*keys):
        for k in keys:
            if bot.get(k) not in (None, ""):
                return bot[k]
        return None

    return {
        "bot_id": pick("bot_id", "BotID", "id"),
        "name": pick("bot_name", "BotUsername", "name"),
        "status": pick("status", "Status"),
        "platform": pick("platform", "Platform"),
        "meeting_link": pick("meeting_link", "MeetingLink", "meeting_url"),
        "created_at": pick("created_at", "CreatedAt"),
        "duration": pick("duration", "Duration"),
        "custom_attributes": pick("custom_attributes") or {},
    }
