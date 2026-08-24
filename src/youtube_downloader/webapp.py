"""A local web UI for the downloader.

A browser cannot fetch YouTube's streams by itself: YouTube serves no
cross-origin headers, so page JavaScript is not allowed to read them. This
module closes that gap with a small HTTP server that runs on the user's own
machine, does the downloading with yt-dlp, and serves the single-page UI in
``static/index.html``.

The server binds to loopback by default and every API call must carry a token
minted at startup, so a web page the user happens to be visiting cannot drive
it.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import secrets
import shutil
import socket
import threading
import uuid
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, quote, urlparse

from .downloader import Progress, YouTubeDownloader
from .errors import YouTubeDownloaderError
from .options import DownloadOptions

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
MAX_BODY_BYTES = 64 * 1024


@dataclass
class JobFile:
    """One file produced by a job."""

    name: str
    size: int | None
    path: Path

    def as_dict(self, index: int) -> dict[str, Any]:
        return {"index": index, "name": self.name, "size": self.size}


@dataclass
class Job:
    """A download running in the background, as the browser sees it."""

    id: str
    url: str
    mode: str
    status: str = "queued"
    title: str | None = None
    percent: float | None = None
    speed: float | None = None
    eta: int | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    error: str | None = None
    files: list[JobFile] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "percent": self.percent,
            "speed": self.speed,
            "eta": self.eta,
            "downloadedBytes": self.downloaded_bytes,
            "totalBytes": self.total_bytes,
            "error": self.error,
            "files": [f.as_dict(i) for i, f in enumerate(self.files)],
        }


class JobRegistry:
    """Thread-safe store of the jobs this server has run."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def create(self, url: str, mode: str) -> Job:
        job = Job(id=uuid.uuid4().hex, url=url, mode=mode)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    def remove(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._jobs:
                return False
            del self._jobs[job_id]
            self._order.remove(job_id)
            return True

    def update(self, job: Job, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)


def _options_from_request(base: DownloadOptions, payload: dict[str, Any]) -> DownloadOptions:
    """Apply the browser's form values on top of the server's base options."""
    audio = payload.get("mode") == "audio"
    changes: dict[str, Any] = {
        "audio_only": audio,
        "playlist": bool(payload.get("playlist")),
        "subtitles": bool(payload.get("subtitles")),
        "embed_subtitles": bool(payload.get("subtitles")) and not audio,
    }
    if not audio:
        if payload.get("quality"):
            changes["quality"] = str(payload["quality"])
        if payload.get("container"):
            changes["container"] = str(payload["container"])
    else:
        if payload.get("audioFormat"):
            changes["audio_format"] = str(payload["audioFormat"])
        if payload.get("bitrate"):
            changes["audio_bitrate"] = str(payload["bitrate"])
    return replace(base, **changes)


class DownloadService:
    """Runs downloads in background threads and records their progress."""

    def __init__(self, options: DownloadOptions, *, allow_other_sites: bool = False) -> None:
        self.options = options
        self.allow_other_sites = allow_other_sites
        self.jobs = JobRegistry()
        self._threads: list[threading.Thread] = []

    def probe(self, url: str, *, allow_other_sites: bool | None = None) -> dict[str, Any]:
        downloader = YouTubeDownloader(
            self.options,
            allow_other_sites=self.allow_other_sites if allow_other_sites is None else allow_other_sites,
        )
        info = downloader.probe(url)
        return {
            "id": info.video_id,
            "title": info.title,
            "url": info.url,
            "uploader": info.uploader,
            "duration": info.duration,
            "durationText": info.duration_str,
            "uploadDate": info.upload_date,
            "viewCount": info.view_count,
            "thumbnail": info.thumbnail,
            "formats": [
                {
                    "id": f.format_id,
                    "ext": f.ext,
                    "resolution": f.resolution,
                    "fps": f.fps,
                    "filesize": f.filesize,
                    "note": f.note,
                    "hasVideo": f.has_video,
                    "hasAudio": f.has_audio,
                }
                for f in info.formats
            ],
        }

    def start(self, payload: dict[str, Any]) -> Job:
        url = str(payload.get("url", "")).strip()
        mode = "audio" if payload.get("mode") == "audio" else "video"
        options = _options_from_request(self.options, payload)
        allow_other = bool(payload.get("anySite", self.allow_other_sites))
        job = self.jobs.create(url, mode)

        thread = threading.Thread(
            target=self._run, args=(job, url, options, allow_other), daemon=True
        )
        self._threads.append(thread)
        thread.start()
        return job

    def _run(
        self, job: Job, url: str, options: DownloadOptions, allow_other: bool
    ) -> None:
        self.jobs.update(job, status="running")

        def on_progress(progress: Progress) -> None:
            if progress.status == "downloading":
                self.jobs.update(
                    job,
                    percent=progress.percent,
                    speed=progress.speed,
                    eta=progress.eta,
                    downloaded_bytes=progress.downloaded_bytes,
                    total_bytes=progress.total_bytes,
                )
            elif progress.status == "finished":
                self.jobs.update(job, percent=100.0, speed=None, eta=None)

        try:
            downloader = YouTubeDownloader(options, allow_other_sites=allow_other)
            results = downloader.download(url, progress=on_progress)
        except YouTubeDownloaderError as exc:
            self.jobs.update(job, status="error", error=str(exc))
            return
        except Exception as exc:  # pragma: no cover - unexpected yt-dlp failure
            logger.exception("download failed")
            self.jobs.update(job, status="error", error=f"unexpected error: {exc}")
            return

        files = [
            JobFile(name=r.path.name, size=r.filesize, path=r.path)
            for r in results
            if r.succeeded and r.path is not None
        ]
        title = next((r.title for r in results if r.title), None)
        if files:
            self.jobs.update(job, status="done", files=files, title=title, percent=100.0)
        elif results:
            self.jobs.update(
                job,
                status="skipped",
                title=title,
                error="nothing to download: already downloaded, or skipped by the archive",
            )
        else:
            self.jobs.update(job, status="error", error="no video found at that URL")


#: Anything outside printable ASCII cannot go in a header value unescaped.
_HEADER_UNSAFE = re.compile(r"[^\x20-\x7e]")


def content_disposition(name: str) -> str:
    """Build a ``Content-Disposition`` header value for a downloaded file.

    Two problems make the obvious version wrong. Video titles routinely
    contain characters outside latin-1 — yt-dlp rewrites characters that are
    illegal in filenames into fullwidth forms — and ``http.server`` encodes
    headers as latin-1 strict, so those raise ``UnicodeEncodeError`` and kill
    the response before any of the file is sent. Titles are also chosen by
    whoever uploaded the video, so a newline in one must never reach a header.

    ``filename*`` carries the real name per RFC 5987; the quoted ``filename``
    is a sanitised ASCII fallback.
    """
    fallback = _HEADER_UNSAFE.sub("_", name).replace('"', "'").replace("\\", "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _read_index() -> bytes:
    return INDEX_FILE.read_bytes()


class _Handler(BaseHTTPRequestHandler):
    server_version = "youtube-downloader"
    protocol_version = "HTTP/1.1"

    service: DownloadService
    token: str

    # -- plumbing ------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        supplied = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(supplied, self.token)

    # -- routes --------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path in {"/", "/index.html"}:
            self._send(HTTPStatus.OK, _read_index(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return

        if not self._authorised(query):
            self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
            return

        if path == "/api/jobs":
            self._json(HTTPStatus.OK, {"jobs": [j.as_dict() for j in self.service.jobs.all()]})
            return
        if path.startswith("/api/jobs/"):
            job = self.service.jobs.get(path.removeprefix("/api/jobs/"))
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, "no such job")
                return
            self._json(HTTPStatus.OK, job.as_dict())
            return
        if path.startswith("/api/files/"):
            self._serve_file(path.removeprefix("/api/files/"))
            return
        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorised(parse_qs(parsed.query)):
            self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
            return
        try:
            payload = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        url = str(payload.get("url", "")).strip()
        if not url:
            self._error(HTTPStatus.BAD_REQUEST, "a url is required")
            return

        if parsed.path == "/api/info":
            try:
                self._json(HTTPStatus.OK, self.service.probe(url, allow_other_sites=payload.get("anySite")))
            except YouTubeDownloaderError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/download":
            self._json(HTTPStatus.ACCEPTED, self.service.start(payload).as_dict())
            return
        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorised(parse_qs(parsed.query)):
            self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
            return
        if parsed.path.startswith("/api/jobs/"):
            removed = self.service.jobs.remove(parsed.path.removeprefix("/api/jobs/"))
            status = HTTPStatus.OK if removed else HTTPStatus.NOT_FOUND
            self._json(status, {"removed": removed})
            return
        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _serve_file(self, reference: str) -> None:
        job_id, _, index_text = reference.partition("/")
        job = self.service.jobs.get(job_id)
        if job is None:
            self._error(HTTPStatus.NOT_FOUND, "no such job")
            return
        try:
            job_file = job.files[int(index_text)]
        except (ValueError, IndexError):
            self._error(HTTPStatus.NOT_FOUND, "no such file")
            return

        # Only ever serve files this server itself recorded, and only from
        # inside the configured output directory.
        root = Path(self.service.options.output_dir).resolve()
        path = job_file.path.resolve()
        if not path.is_file() or not path.is_relative_to(root):
            self._error(HTTPStatus.NOT_FOUND, "file is no longer available")
            return

        self._stream_file(path)

    def _stream_file(self, path: Path) -> None:
        """Send a file without holding it in memory.

        Videos are routinely larger than it is reasonable to buffer, so the
        body is copied in chunks straight to the socket.
        """
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", content_disposition(path.name))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, 256 * 1024)


LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD = frozenset({"0.0.0.0", "::"})


def lan_addresses() -> list[str]:
    """Best-effort list of non-loopback addresses this machine answers on.

    Used only to print a URL a phone on the same network can actually reach,
    so a partial or empty answer is fine.
    """
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # Asks the OS which local address would be used to reach the
            # outside world. UDP connect sends no packets.
            probe.connect(("8.8.8.8", 53))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
        found.extend(addresses)
    except OSError:
        pass

    unique: list[str] = []
    for address in found:
        if address not in unique and not address.startswith("127."):
            unique.append(address)
    return unique


def serve_urls(
    host: str, port: int, token: str, addresses: Sequence[str] | None = None
) -> list[tuple[str, str]]:
    """The (label, url) pairs worth printing when the server starts.

    Binding to a wildcard or an explicit network address means the point is to
    reach it from another device, so the reachable address is what the user
    needs — printing only the loopback URL would be useless to them.
    """
    def url_for(address: str) -> str:
        shown = f"[{address}]" if ":" in address else address
        return f"http://{shown}:{port}/?t={token}"

    if host in LOOPBACK:
        return [("On this machine", url_for("127.0.0.1"))]
    if host not in WILDCARD:
        # Binding one address means loopback is NOT bound, so a 127.0.0.1 link
        # would be dead — and it is the link the browser would be opened on.
        return [("Reachable at", url_for(host))]

    pairs = [("On this machine", url_for("127.0.0.1"))]
    for address in list(addresses) if addresses is not None else lan_addresses():
        pairs.append(("On another device", url_for(address)))
    return pairs


def create_server(
    options: DownloadOptions,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    allow_other_sites: bool = False,
) -> tuple[ThreadingHTTPServer, str]:
    """Build the local server. Returns the server and its access token."""
    service = DownloadService(options, allow_other_sites=allow_other_sites)
    access_token = token or secrets.token_urlsafe(24)

    handler = type(
        "BoundHandler", (_Handler,), {"service": service, "token": access_token}
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server, access_token
