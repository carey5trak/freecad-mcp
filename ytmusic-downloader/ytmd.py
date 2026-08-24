#!/usr/bin/env python3
"""
ytmd - local helper service for the YouTube Music Playlist Downloader HTML app.

A browser page cannot fetch YouTube media on its own (CORS, signature/throttling),
so the HTML app talks to this small localhost-only HTTP service, which drives
yt-dlp.

Run it with:

    python3 ytmd.py

...then open the URL it prints. Nothing is exposed off the machine: the socket is
bound to the loopback interface, the Host header is pinned to loopback names, the
Origin header is checked, and every /api/ call needs a per-process token that is
only ever handed to pages this service itself served.

Responsible use: only download material you have the rights to keep (your own
uploads, Creative Commons / public-domain works, or content you are licensed
for). Downloading copyrighted music you do not have rights to breaks YouTube's
Terms of Service.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import platform
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, quote, urlparse

APP_NAME = "YouTube Music Playlist Downloader"
APP_VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"

# Hard ceilings. These exist so a stray request cannot make the process
# allocate without bound; they are not a security boundary on their own.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RESOLVE_ENTRIES = 10_000
MAX_JOB_ITEMS = 5_000
MAX_LOG_LINES = 2_000
SSE_HEARTBEAT_SECONDS = 15.0
PROGRESS_THROTTLE_SECONDS = 0.25

LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

# --------------------------------------------------------------------------- #
# yt-dlp import
# --------------------------------------------------------------------------- #

try:
    import yt_dlp
    from yt_dlp.utils import DownloadError, sanitize_filename
except ImportError as _import_error:  # pragma: no cover - needs a broken install
    if (_import_error.name or "").split(".")[0] == "yt_dlp":
        sys.stderr.write(
            "\n  yt-dlp is not installed.\n\n"
            "  Install it with:  python3 -m pip install -r requirements.txt\n"
            "              or:  python3 -m pip install --upgrade yt-dlp\n\n"
        )
        raise SystemExit(2)
    # Something yt-dlp itself needs is missing, or a local file is shadowing a
    # stdlib module. Say so rather than blaming the wrong package.
    sys.stderr.write(
        f"\n  yt-dlp could not be loaded: {_import_error}\n\n"
        f"  A module named {_import_error.name!r} failed to import. If a file in the\n"
        "  current directory shares its name with a standard library module, rename it.\n\n"
    )
    raise

try:  # DownloadCancelled is re-raised untouched by YoutubeDL's error handling.
    from yt_dlp.utils import DownloadCancelled as _DownloadCancelledBase
except ImportError:  # pragma: no cover - very old yt-dlp
    _DownloadCancelledBase = Exception  # type: ignore[assignment,misc]

try:
    from yt_dlp.postprocessor.metadataparser import MetadataParserPP

    _TITLE_INTERPRETER = (
        MetadataParserPP.Actions.INTERPRET,
        "title",
        r"(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+)",
    )
except Exception:  # pragma: no cover - keeps the app usable on odd builds
    MetadataParserPP = None  # type: ignore[assignment]
    _TITLE_INTERPRETER = None  # type: ignore[assignment]


class Cancelled(_DownloadCancelledBase):  # type: ignore[misc,valid-type]
    """Raised from progress hooks to unwind an in-flight download."""


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def default_output_dir() -> str:
    home = Path.home()
    music = home / "Music"
    base = music if music.is_dir() else home / "Downloads"
    return str(base / "YouTube Music")


def config_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "ytmd"


DEFAULT_SETTINGS: dict[str, Any] = {
    # Output
    "outputDir": "",  # filled in at load time
    "folderMode": "playlist",  # playlist | artist | flat | custom
    "customFolder": "",
    "fileTemplate": "%(title)s.%(ext)s",
    "numberTracks": True,
    "asciiFilenames": False,
    # Format
    "mode": "audio",  # audio | video
    "audioFormat": "mp3",  # best | mp3 | m4a | opus | flac | wav | vorbis
    "audioQuality": "0",  # "0" = best VBR, otherwise kbps
    "videoQuality": "1080",  # best | 2160 | 1440 | 1080 | 720 | 480
    "videoContainer": "mp4",  # mp4 | mkv | webm
    # Extras
    "embedThumbnail": True,
    "embedMetadata": True,
    "writeSubtitles": False,
    "parseArtistFromTitle": False,
    "skipNonMusic": False,  # SponsorBlock music_offtopic
    "useArchive": True,
    "writePlaylistFile": False,
    # Network
    "concurrency": 3,
    "rateLimitKbps": 0,  # 0 = unlimited
    "politeThrottle": True,
    "retries": 5,
    "cookiesFrom": "none",  # none | file | chrome | firefox | edge | brave | chromium | opera | vivaldi | safari
    "cookieFile": "",
    "ffmpegLocation": "",
    "jsRuntime": "auto",  # auto | deno | node | quickjs | bun
    # UI-only, persisted here so the app survives a browser cache wipe
    "theme": "system",
    "showThumbnails": True,
    "noticeAccepted": False,
}

_BOOL_KEYS = {k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, bool)}
_INT_KEYS = {k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, int) and not isinstance(v, bool)}
_ENUMS: dict[str, frozenset[str]] = {
    "folderMode": frozenset({"playlist", "artist", "flat", "custom"}),
    "mode": frozenset({"audio", "video"}),
    "audioFormat": frozenset({"best", "mp3", "m4a", "opus", "flac", "wav", "vorbis"}),
    "audioQuality": frozenset({"0", "320", "256", "192", "160", "128", "96"}),
    "videoQuality": frozenset({"best", "2160", "1440", "1080", "720", "480", "360"}),
    "videoContainer": frozenset({"mp4", "mkv", "webm"}),
    "cookiesFrom": frozenset(
        {"none", "file", "chrome", "chromium", "firefox", "edge", "brave", "opera", "vivaldi", "safari"}
    ),
    "jsRuntime": frozenset({"auto", "deno", "node", "quickjs", "bun"}),
    "theme": frozenset({"system", "dark", "light"}),
}
_CLAMPS: dict[str, tuple[int, int]] = {
    "concurrency": (1, 8),
    "rateLimitKbps": (0, 1_000_000),
    "retries": (0, 20),
}
# Anything the user can type that ends up in a shell-free subprocess or a
# filesystem path still needs a length bound so the UI cannot wedge the service.
_MAX_STRING_LEN = 4096


def coerce_settings(raw: Any, base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Merge client-supplied settings onto a known-good base, dropping junk.

    Unknown keys are discarded, values are coerced to the type of the default,
    enums are validated and numbers are clamped. The result is always safe to
    hand to the download engine.
    """
    merged = dict(base or DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return merged
    for key, default in DEFAULT_SETTINGS.items():
        if key not in raw:
            continue
        value = raw[key]
        if key in _BOOL_KEYS:
            merged[key] = bool(value)
        elif key in _INT_KEYS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            low, high = _CLAMPS.get(key, (-(2**31), 2**31))
            merged[key] = max(low, min(high, number))
        else:
            if value is None:
                value = ""
            if not isinstance(value, (str, int, float)):
                continue
            text = str(value)[:_MAX_STRING_LEN]
            allowed = _ENUMS.get(key)
            if allowed is not None and text not in allowed:
                continue
            merged[key] = text
    return merged


class SettingsStore:
    """Settings persisted as JSON, so the app remembers choices between runs."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (config_dir() / "settings.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        data["outputDir"] = default_output_dir()
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return data
        return coerce_settings(stored, data)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, raw: Any) -> dict[str, Any]:
        with self._lock:
            self._data = coerce_settings(raw, self._data)
            snapshot = dict(self._data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), "utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log(f"could not save settings to {self.path}: {exc}")
        return snapshot


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_LOG_LOCK = threading.Lock()


def log(message: str) -> None:
    with _LOG_LOCK:
        sys.stderr.write(f"[ytmd] {message}\n")
        sys.stderr.flush()


def find_ffmpeg(configured: str = "") -> Optional[str]:
    """Resolve an ffmpeg binary, accepting either a file or its directory."""
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            for name in ("ffmpeg", "ffmpeg.exe"):
                if (candidate / name).exists():
                    return str(candidate)
        elif candidate.exists():
            return str(candidate.parent)
    found = shutil.which("ffmpeg")
    return str(Path(found).parent) if found else None


# yt-dlp's priority order; the first available one is what it will actually use.
JS_RUNTIME_PRIORITY = ("deno", "node", "quickjs", "bun")


@functools.lru_cache(maxsize=1)
def detect_js_runtimes() -> dict[str, str]:
    """Which JavaScript runtimes can yt-dlp actually use on this machine?

    Recent yt-dlp needs one to extract from YouTube at all, and only Deno is
    enabled by default - so a machine with Node but no Deno looks broken until
    the runtime is named explicitly.
    """
    found: dict[str, str] = {}
    try:
        from yt_dlp.globals import supported_js_runtimes

        for name, runtime_cls in supported_js_runtimes.value.items():
            try:
                info = runtime_cls().info
            except Exception:  # noqa: BLE001 - probing must never be fatal
                continue
            if info is not None:
                found[name] = getattr(info, "version", "") or "present"
    except Exception:  # noqa: BLE001 - older yt-dlp has no runtime plugins
        pass
    if not found:  # fall back to a plain PATH lookup
        for name in JS_RUNTIME_PRIORITY:
            if shutil.which(name):
                found[name] = "present"
    return found


def chosen_js_runtime(settings: dict[str, Any]) -> str:
    """The runtime yt-dlp will end up using, or '' if there is none."""
    preference = settings.get("jsRuntime", "auto")
    available = detect_js_runtimes()
    if preference != "auto":
        return preference if preference in available else ""
    for name in JS_RUNTIME_PRIORITY:
        if name in available:
            return name
    return ""


def apply_js_runtime_opts(opts: dict[str, Any], settings: dict[str, Any]) -> None:
    """Enable a non-default runtime when that is the only one installed."""
    name = chosen_js_runtime(settings)
    # yt-dlp already defaults to deno, so only speak up for the others.
    if name and name != "deno":
        opts["js_runtimes"] = {name: {}}


def safe_component(name: str, ascii_only: bool = False) -> str:
    """Turn arbitrary text into one filesystem-safe path component."""
    cleaned = sanitize_filename(name or "", restricted=ascii_only)
    cleaned = cleaned.strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "Unknown"
    # Windows reserves these regardless of extension.
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", cleaned):
        cleaned = f"_{cleaned}"
    return cleaned[:120]


def escape_template_literal(text: str) -> str:
    """Escape text so yt-dlp's output template treats it as a literal."""
    return text.replace("%", "%%")


_UID_SAFE = re.compile(r"[^A-Za-z0-9:_~.-]")


def sanitize_uid(value: Any) -> str:
    """Accept the page's row key, bounded so it stays usable in a URL path."""
    if not isinstance(value, str):
        return ""
    return _UID_SAFE.sub("", value)[:80]


def canonical_watch_url(video_id: str, fallback: str = "") -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        return f"https://www.youtube.com/watch?v={video_id}"
    return fallback


def is_unavailable_title(title: str) -> bool:
    return bool(re.fullmatch(r"\[(private|deleted|unavailable)[^\]]*\]", (title or "").strip(), re.I))


def thumbnail_for(entry: dict[str, Any]) -> str:
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        # Prefer a mid-size thumbnail; the list is ordered worst -> best.
        chosen = thumbs[min(len(thumbs) - 1, 1)]
        if isinstance(chosen, dict) and isinstance(chosen.get("url"), str):
            return chosen["url"]
    single = entry.get("thumbnail")
    if isinstance(single, str):
        return single
    vid = entry.get("id")
    if isinstance(vid, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    return ""


class _CollectingLogger:
    """Routes yt-dlp's own output into a per-item callback."""

    def __init__(self, sink: Callable[[str, str], None]) -> None:
        self._sink = sink

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            return
        self._sink("debug", msg)

    def info(self, msg: str) -> None:
        self._sink("info", msg)

    def warning(self, msg: str) -> None:
        self._sink("warning", msg)

    def error(self, msg: str) -> None:
        self._sink("error", msg)


# --------------------------------------------------------------------------- #
# Resolving a URL into a track list
# --------------------------------------------------------------------------- #

SEARCH_PREFIX_RE = re.compile(r"^(ytsearch\d*|ytsearchall):", re.I)


def normalize_source(text: str) -> str:
    """Accept a URL, a bare video/playlist id, or a free-text search."""
    text = (text or "").strip()
    if not text:
        return ""
    if SEARCH_PREFIX_RE.match(text):
        return text
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        return text
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return f"https://www.youtube.com/watch?v={text}"
    if re.fullmatch(r"(PL|OL|UU|LL|FL|RD|OLAK5uy_)[A-Za-z0-9_-]{6,}", text):
        return f"https://www.youtube.com/playlist?list={text}"
    # Anything else is treated as a YouTube search.
    return f"ytsearch25:{text}"


def _flatten_entries(node: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    """Walk a (possibly nested) yt-dlp result, collecting playable entries."""
    if node is None or len(out) >= MAX_RESOLVE_ENTRIES:
        return
    if isinstance(node, dict) and node.get("_type") in {None, "video", "url", "url_transparent"}:
        if node.get("_type") in {None, "video"} or node.get("ie_key") in {None, "Youtube"} or node.get("url"):
            out.append(node)
            return
    entries = node.get("entries") if isinstance(node, dict) else None
    if entries is None:
        return
    if depth > 3:
        return
    for child in entries:
        if len(out) >= MAX_RESOLVE_ENTRIES:
            return
        _flatten_entries(child, out, depth + 1)


def resolve_source(raw_url: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Extract a playlist/video/search into a flat, UI-ready track list."""
    source = normalize_source(raw_url)
    if not source:
        raise ValueError("Enter a playlist URL, a video URL, or something to search for.")

    messages: list[tuple[str, str]] = []
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "playlistend": MAX_RESOLVE_ENTRIES,
        "logger": _CollectingLogger(
            lambda level, msg: messages.append((level, msg)) if level in {"warning", "error"} else None
        ),
    }
    apply_cookie_opts(opts, settings)
    apply_js_runtime_opts(opts, settings)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source, download=False)
    if info is None:
        # ignoreerrors means yt-dlp hands back None instead of raising, so the
        # real reason only exists in the log we just collected.
        detail = next((clean_error(m) for level, m in messages if level == "error"), "")
        if not detail:
            detail = next((clean_error(m) for level, m in messages if level == "warning"), "")
        hints = []
        if not detect_js_runtimes():
            hints.append(
                "No JavaScript runtime was found - current yt-dlp needs one to read YouTube. "
                "Install Deno (or Node) and pick it under Options."
            )
        hints.append("If this is a private or 'Liked music' playlist, set a cookie source in Options.")
        raise ValueError(" ".join(["yt-dlp could not read that link.", detail, *hints]).replace("  ", " ").strip())
    info = yt_dlp.YoutubeDL().sanitize_info(info)

    raw_entries: list[dict[str, Any]] = []
    _flatten_entries(info, raw_entries)
    if not raw_entries and info.get("_type") in {None, "video"}:
        raw_entries = [info]

    tracks: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_entries, start=1):
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "")
        title = str(entry.get("title") or entry.get("fulltitle") or "Untitled")
        url = canonical_watch_url(video_id, str(entry.get("url") or entry.get("webpage_url") or ""))
        if not url:
            continue
        duration = entry.get("duration")
        tracks.append(
            {
                "id": video_id or f"idx{index}",
                "index": index,
                "title": title,
                "uploader": str(
                    entry.get("artist")
                    or entry.get("creator")
                    or entry.get("uploader")
                    or entry.get("channel")
                    or ""
                ),
                "duration": int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
                "url": url,
                "thumbnail": thumbnail_for(entry) if settings.get("showThumbnails", True) else "",
                "available": not is_unavailable_title(title),
            }
        )

    is_playlist = info.get("_type") in {"playlist", "multi_video"} or len(tracks) > 1
    playlist_title = str(info.get("title") or "") if is_playlist else ""
    if SEARCH_PREFIX_RE.match(source) or source.startswith("ytsearch"):
        playlist_title = playlist_title or f"Search: {raw_url.strip()}"

    return {
        "kind": "playlist" if is_playlist else "video",
        "source": source,
        "title": playlist_title or (tracks[0]["title"] if tracks else "Nothing found"),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "thumbnail": thumbnail_for(info) if settings.get("showThumbnails", True) else "",
        "webpageUrl": str(info.get("webpage_url") or source),
        "count": len(tracks),
        "totalDuration": sum(t["duration"] or 0 for t in tracks),
        "tracks": tracks,
        "warnings": [m for _level, m in messages][:20],
        "truncated": len(tracks) >= MAX_RESOLVE_ENTRIES,
    }


# --------------------------------------------------------------------------- #
# yt-dlp option construction
# --------------------------------------------------------------------------- #


def apply_cookie_opts(opts: dict[str, Any], settings: dict[str, Any]) -> None:
    source = settings.get("cookiesFrom", "none")
    if source == "file":
        path = (settings.get("cookieFile") or "").strip()
        if path:
            opts["cookiefile"] = str(Path(path).expanduser())
    elif source not in {"none", ""}:
        # (browser, profile, keyring, container) - only the browser is needed.
        opts["cookiesfrombrowser"] = (source, None, None, None)


def build_postprocessors(settings: dict[str, Any], has_ffmpeg: bool) -> list[dict[str, Any]]:
    """Assemble the postprocessor chain in the order yt-dlp expects."""
    chain: list[dict[str, Any]] = []
    if not has_ffmpeg:
        # Every postprocessor below shells out to ffmpeg; without it we can only
        # keep whatever stream YouTube served.
        return chain

    if settings.get("parseArtistFromTitle") and _TITLE_INTERPRETER is not None:
        chain.append({"key": "MetadataParser", "when": "pre_process", "actions": [_TITLE_INTERPRETER]})

    if settings.get("skipNonMusic"):
        chain.append({"key": "SponsorBlock", "categories": ["music_offtopic"], "when": "after_filter"})
        chain.append({"key": "ModifyChapters", "remove_sponsor_segments": ["music_offtopic"]})

    if settings.get("mode") == "audio":
        audio_format = settings.get("audioFormat", "mp3")
        if audio_format != "best":
            chain.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": settings.get("audioQuality", "0"),
                    "nopostoverwrites": False,
                }
            )

    if settings.get("embedMetadata"):
        chain.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True, "add_infojson": False})

    if settings.get("writeSubtitles") and settings.get("mode") == "video":
        chain.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    if settings.get("embedThumbnail"):
        chain.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    return chain


def build_format_selector(settings: dict[str, Any], has_ffmpeg: bool) -> tuple[str, Optional[str]]:
    """Return (format string, merge container)."""
    if settings.get("mode") == "audio":
        return "bestaudio/best", None

    quality = settings.get("videoQuality", "1080")
    container = settings.get("videoContainer", "mp4")
    if not has_ffmpeg:
        # No muxer available, so only pre-merged (progressive) streams work.
        return ("best" if quality == "best" else f"best[height<={quality}]/best"), None
    if quality == "best":
        return "bestvideo*+bestaudio/best", container
    return (
        f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
        container,
    )


def resolve_target_dir(settings: dict[str, Any], playlist_title: str, track: dict[str, Any]) -> Path:
    base = Path((settings.get("outputDir") or default_output_dir())).expanduser()
    ascii_only = bool(settings.get("asciiFilenames"))
    mode = settings.get("folderMode", "playlist")
    if mode == "flat":
        return base
    if mode == "custom":
        raw = (settings.get("customFolder") or "").strip()
        if not raw:
            return base
        # Allow nested folders but never let a component escape the base dir.
        parts = [safe_component(p, ascii_only) for p in re.split(r"[\\/]+", raw) if p.strip()]
        return base.joinpath(*parts) if parts else base
    if mode == "artist":
        return base / safe_component(track.get("uploader") or "Unknown Artist", ascii_only)
    return base / safe_component(playlist_title or "Downloads", ascii_only)


def build_ydl_opts(
    settings: dict[str, Any],
    track: dict[str, Any],
    playlist_title: str,
    has_ffmpeg: bool,
    ffmpeg_dir: Optional[str],
    hooks: dict[str, Any],
    archive_path: Optional[Path],
) -> dict[str, Any]:
    target_dir = resolve_target_dir(settings, playlist_title, track)
    ascii_only = bool(settings.get("asciiFilenames"))

    prefix = ""
    if settings.get("numberTracks"):
        prefix = escape_template_literal(f"{int(track.get('index') or 1):02d} - ")
    filename_template = (settings.get("fileTemplate") or "%(title)s.%(ext)s").strip() or "%(title)s.%(ext)s"
    if "%(ext)s" not in filename_template:
        filename_template = f"{filename_template}.%(ext)s"

    fmt, merge_container = build_format_selector(settings, has_ffmpeg)

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "noplaylist": True,  # a /watch?v=..&list=.. link must stay a single track
        "ignoreerrors": False,
        "consoletitle": False,
        "color": "no_color",
        "format": fmt,
        "outtmpl": {"default": str(target_dir / f"{prefix}{filename_template}")},
        "paths": {"temp": str(target_dir / ".ytmd-part")},
        "restrictfilenames": ascii_only,
        "windowsfilenames": sys.platform == "win32",
        "overwrites": False,
        "continuedl": True,
        "retries": int(settings.get("retries", 5)),
        "fragment_retries": int(settings.get("retries", 5)),
        "extractor_retries": min(3, int(settings.get("retries", 5))),
        "concurrent_fragment_downloads": 4,
        "postprocessors": build_postprocessors(settings, has_ffmpeg),
        "writethumbnail": bool(settings.get("embedThumbnail")) and has_ffmpeg,
        "progress_hooks": [hooks["progress"]],
        "postprocessor_hooks": [hooks["postprocessor"]],
        "logger": hooks["logger"],
        "trim_file_name": 200,
    }

    if merge_container:
        opts["merge_output_format"] = merge_container
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir
    if settings.get("writeSubtitles") and settings.get("mode") == "video":
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en.*", "-live_chat"]
    rate = int(settings.get("rateLimitKbps", 0))
    if rate > 0:
        opts["ratelimit"] = rate * 1024
    if settings.get("politeThrottle"):
        opts["sleep_interval"] = 1
        opts["max_sleep_interval"] = 3
        opts["sleep_interval_requests"] = 1
    if archive_path is not None:
        opts["download_archive"] = str(archive_path)

    apply_cookie_opts(opts, settings)
    apply_js_runtime_opts(opts, settings)
    return opts


# --------------------------------------------------------------------------- #
# Job model
# --------------------------------------------------------------------------- #

TERMINAL_STATES = frozenset({"done", "error", "skipped", "cancelled"})


class Item:
    __slots__ = (
        "uid", "video_id", "title", "uploader", "url", "index", "thumbnail",
        "state", "progress", "downloaded", "total", "speed", "eta", "stage",
        "message", "filepath", "cancel",
    )

    def __init__(self, track: dict[str, Any], position: int) -> None:
        # The page keys its rows on a uid derived from the position in the FULL
        # resolved playlist. This job only receives the SELECTED tracks, so
        # re-deriving the uid here would disagree with the page for any
        # selection that is not a prefix, and progress would land nowhere.
        # Honour the uid the page sent whenever it gives us one.
        self.uid = sanitize_uid(track.get("uid")) or f"{position}:{track.get('id') or uuid.uuid4().hex[:8]}"
        self.video_id = str(track.get("id") or "")
        self.title = str(track.get("title") or "Untitled")
        self.uploader = str(track.get("uploader") or "")
        self.url = str(track.get("url") or "")
        self.index = int(track.get("index") or position + 1)
        self.thumbnail = str(track.get("thumbnail") or "")
        self.state = "queued"
        self.progress = 0.0
        self.downloaded = 0
        self.total = 0
        self.speed = 0.0
        self.eta = 0
        self.stage = ""
        self.message = ""
        self.filepath = ""
        self.cancel = threading.Event()

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "id": self.video_id,
            "title": self.title,
            "uploader": self.uploader,
            "index": self.index,
            "thumbnail": self.thumbnail,
            "state": self.state,
            "progress": round(self.progress, 4),
            "downloaded": self.downloaded,
            "total": self.total,
            "speed": round(self.speed, 1),
            "eta": self.eta,
            "stage": self.stage,
            "message": self.message,
            "filepath": self.filepath,
        }


class Job:
    def __init__(self, job_id: str, tracks: list[dict[str, Any]], settings: dict[str, Any], playlist_title: str) -> None:
        self.id = job_id
        self.settings = settings
        self.playlist_title = playlist_title
        self.items = []
        taken: set[str] = set()
        for position, track in enumerate(tracks):
            item = Item(track, position)
            while item.uid in taken:  # a duplicate would shadow a row's progress
                item.uid = f"{item.uid}~{position}"
            taken.add(item.uid)
            self.items.append(item)
        self.by_uid = {item.uid: item for item in self.items}
        self.state = "running"
        self.created = time.time()
        self.finished_at: Optional[float] = None
        self.cancel_all = threading.Event()
        self.log: list[dict[str, Any]] = []
        self.log_seq = 0
        self.listeners: list["queue.Queue[Optional[dict[str, Any]]]"] = []
        self.lock = threading.Lock()
        self.executor: Optional[ThreadPoolExecutor] = None
        self.written_files: list[str] = []

    # -- event fan-out ----------------------------------------------------- #

    def subscribe(self) -> "queue.Queue[Optional[dict[str, Any]]]":
        listener: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue(maxsize=1000)
        with self.lock:
            self.listeners.append(listener)
        return listener

    def unsubscribe(self, listener: "queue.Queue[Optional[dict[str, Any]]]") -> None:
        with self.lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            listeners = list(self.listeners)
        for listener in listeners:
            try:
                listener.put_nowait(event)
            except queue.Full:
                # A stalled reader must never slow the download loop down.
                pass

    def add_log(self, level: str, message: str, item_uid: str = "") -> None:
        with self.lock:
            self.log_seq += 1
            entry = {"seq": self.log_seq, "t": time.time(), "level": level, "message": message[:2000], "item": item_uid}
            self.log.append(entry)
            if len(self.log) > MAX_LOG_LINES:
                del self.log[: len(self.log) - MAX_LOG_LINES]
        self.emit({"type": "log", **entry})

    # -- snapshots ---------------------------------------------------------- #

    def counts(self) -> dict[str, int]:
        tally = {"total": len(self.items), "done": 0, "error": 0, "skipped": 0, "cancelled": 0, "active": 0, "queued": 0}
        for item in self.items:
            if item.state in tally:
                tally[item.state] += 1
            elif item.state in {"downloading", "processing"}:
                tally["active"] += 1
        return tally

    def snapshot(self, include_log: bool = True) -> dict[str, Any]:
        with self.lock:
            log_copy = list(self.log[-400:]) if include_log else []
        return {
            "id": self.id,
            "state": self.state,
            "playlistTitle": self.playlist_title,
            "created": self.created,
            "finishedAt": self.finished_at,
            "counts": self.counts(),
            "items": [item.to_dict() for item in self.items],
            "log": log_copy,
            "outputDir": str(Path(self.settings.get("outputDir") or default_output_dir()).expanduser()),
            "writtenFiles": list(self.written_files),
        }


# --------------------------------------------------------------------------- #
# Download engine
# --------------------------------------------------------------------------- #


class JobManager:
    """Owns every download job and the worker pools that drive them."""

    def __init__(self, settings_store: SettingsStore, max_jobs: int = 20) -> None:
        self.settings_store = settings_store
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.lock = threading.Lock()
        self.max_jobs = max_jobs

    # -- lifecycle ---------------------------------------------------------- #

    def create(self, tracks: list[dict[str, Any]], settings: dict[str, Any], playlist_title: str) -> Job:
        job = Job(uuid.uuid4().hex[:12], tracks, settings, playlist_title)
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            # Keep memory bounded: drop the oldest finished jobs.
            while len(self.order) > self.max_jobs:
                stale = self.order[0]
                if self.jobs.get(stale) and self.jobs[stale].state == "running":
                    break
                self.order.pop(0)
                self.jobs.pop(stale, None)
        threading.Thread(target=self._run, args=(job,), name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        job.cancel_all.set()
        for item in job.items:
            item.cancel.set()
            if item.state == "queued":
                self._set_state(job, item, "cancelled", message="Cancelled before it started")
        job.add_log("info", "Cancelling remaining downloads...")
        return True

    def cancel_item(self, job_id: str, uid: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        item = job.by_uid.get(uid)
        if not item or item.state in TERMINAL_STATES:
            return False
        item.cancel.set()
        if item.state == "queued":
            self._set_state(job, item, "cancelled", message="Cancelled before it started")
        return True

    # -- state plumbing ------------------------------------------------------ #

    def _set_state(self, job: Job, item: Item, state: str, **fields: Any) -> None:
        item.state = state
        for key, value in fields.items():
            setattr(item, key, value)
        if state in TERMINAL_STATES:
            item.speed = 0.0
            item.eta = 0
            if state == "done":
                item.progress = 1.0
        job.emit({"type": "item", "item": item.to_dict(), "counts": job.counts()})

    # -- the worker ---------------------------------------------------------- #

    def _run(self, job: Job) -> None:
        """Guarantee a terminal state: a job stuck on "running" hangs the page,
        which sits waiting for an event stream that will never end."""
        try:
            self._run_job(job)
        except BaseException:
            job.add_log("error", "The download job stopped unexpectedly:\n" + traceback.format_exc(limit=4))
            raise
        finally:
            if job.state == "running":
                for item in job.items:
                    if item.state not in TERMINAL_STATES:
                        item.state = "error"
                        item.message = item.message or "The job stopped before this track finished."
                job.state = "error"
                job.finished_at = time.time()
                job.emit({
                    "type": "job",
                    "job": {"state": job.state, "finishedAt": job.finished_at},
                    "counts": job.counts(),
                })
                job.emit({"type": "end"})

    def _run_job(self, job: Job) -> None:
        settings = job.settings
        ffmpeg_dir = find_ffmpeg(settings.get("ffmpegLocation", ""))
        has_ffmpeg = ffmpeg_dir is not None

        out_dir = Path(settings.get("outputDir") or default_output_dir()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            job.add_log("error", f"Cannot create output folder {out_dir}: {exc}")
            job.state = "error"
            job.finished_at = time.time()
            job.emit({"type": "job", "job": {"state": job.state}, "counts": job.counts()})
            job.emit({"type": "end"})
            return

        archive_path = (out_dir / ".ytmd-archive.txt") if settings.get("useArchive") else None
        if not has_ffmpeg:
            job.add_log(
                "warning",
                "ffmpeg was not found, so audio conversion, tagging and cover art are unavailable. "
                "Files will be saved in whatever format YouTube served.",
            )

        workers = max(1, min(8, int(settings.get("concurrency", 3))))
        job.add_log("info", f"Starting {len(job.items)} download(s) with {workers} worker(s) into {out_dir}")

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"dl-{job.id}") as pool:
            job.executor = pool
            futures = [
                pool.submit(self._download_one, job, item, has_ffmpeg, ffmpeg_dir, archive_path)
                for item in job.items
            ]
            for future in futures:
                try:
                    future.result()
                except Exception:  # a worker must never take the pool down
                    job.add_log("error", traceback.format_exc(limit=3))

        if settings.get("writePlaylistFile") and job.written_files:
            self._write_m3u(job, out_dir)

        counts = job.counts()
        if job.cancel_all.is_set():
            job.state = "cancelled"
        elif counts["error"]:
            job.state = "completed_with_errors"
        else:
            job.state = "completed"
        job.finished_at = time.time()
        job.add_log(
            "info",
            f"Finished: {counts['done']} downloaded, {counts['skipped']} skipped, "
            f"{counts['error']} failed, {counts['cancelled']} cancelled.",
        )
        job.emit({"type": "job", "job": {"state": job.state, "finishedAt": job.finished_at}, "counts": counts})
        job.emit({"type": "end"})

    def _write_m3u(self, job: Job, out_dir: Path) -> None:
        name = safe_component(job.playlist_title or "playlist", bool(job.settings.get("asciiFilenames")))
        target = out_dir / f"{name}.m3u8"
        try:
            lines = ["#EXTM3U"]
            for item in job.items:
                if item.state == "done" and item.filepath:
                    try:
                        rel = os.path.relpath(item.filepath, out_dir)
                    except ValueError:
                        rel = item.filepath
                    lines.append(f"#EXTINF:-1,{item.uploader + ' - ' if item.uploader else ''}{item.title}")
                    lines.append(rel.replace(os.sep, "/"))
            target.write_text("\n".join(lines) + "\n", "utf-8")
            job.add_log("info", f"Wrote playlist file {target}")
        except OSError as exc:
            job.add_log("warning", f"Could not write playlist file: {exc}")

    def _download_one(
        self,
        job: Job,
        item: Item,
        has_ffmpeg: bool,
        ffmpeg_dir: Optional[str],
        archive_path: Optional[Path],
    ) -> None:
        if item.cancel.is_set() or job.cancel_all.is_set():
            if item.state not in TERMINAL_STATES:
                self._set_state(job, item, "cancelled", message="Cancelled")
            return

        last_emit = [0.0]

        def on_progress(status: dict[str, Any]) -> None:
            if item.cancel.is_set() or job.cancel_all.is_set():
                raise Cancelled(f"cancelled: {item.title}")
            phase = status.get("status")
            if phase == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                done = status.get("downloaded_bytes") or 0
                item.downloaded = int(done)
                item.total = int(total)
                if total:
                    item.progress = max(0.0, min(1.0, done / total))
                elif status.get("fragment_count"):
                    item.progress = min(1.0, (status.get("fragment_index") or 0) / status["fragment_count"])
                item.speed = float(status.get("speed") or 0.0)
                item.eta = int(status.get("eta") or 0)
                item.stage = "Downloading"
                if item.state != "downloading":
                    self._set_state(job, item, "downloading")
                    last_emit[0] = time.time()
                    return
                now = time.time()
                if now - last_emit[0] >= PROGRESS_THROTTLE_SECONDS:
                    last_emit[0] = now
                    job.emit({"type": "item", "item": item.to_dict(), "counts": job.counts()})
            elif phase == "finished":
                item.progress = 1.0
                item.downloaded = int(status.get("total_bytes") or item.downloaded)
                item.stage = "Downloaded"
                if isinstance(status.get("filename"), str):
                    item.filepath = status["filename"]
                job.emit({"type": "item", "item": item.to_dict(), "counts": job.counts()})

        def on_postprocessor(status: dict[str, Any]) -> None:
            if item.cancel.is_set() or job.cancel_all.is_set():
                raise Cancelled(f"cancelled: {item.title}")
            name = str(status.get("postprocessor") or "")
            if status.get("status") == "started":
                item.stage = POSTPROCESSOR_LABELS.get(name, name or "Processing")
                if item.state != "processing":
                    self._set_state(job, item, "processing")
                else:
                    job.emit({"type": "item", "item": item.to_dict(), "counts": job.counts()})
            info = status.get("info_dict")
            if isinstance(info, dict) and isinstance(info.get("filepath"), str):
                item.filepath = info["filepath"]

        hooks = {
            "progress": on_progress,
            "postprocessor": on_postprocessor,
            "logger": _CollectingLogger(
                lambda level, msg: job.add_log(level, msg, item.uid) if level in {"warning", "error"} else None
            ),
        }

        opts = build_ydl_opts(
            job.settings, {"index": item.index, "uploader": item.uploader}, job.playlist_title,
            has_ffmpeg, ffmpeg_dir, hooks, archive_path,
        )

        self._set_state(job, item, "downloading", stage="Starting", message="")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(item.url, download=True)
        except Cancelled:
            self._set_state(job, item, "cancelled", message="Cancelled", stage="")
            return
        except DownloadError as exc:
            if item.cancel.is_set() or job.cancel_all.is_set():
                self._set_state(job, item, "cancelled", message="Cancelled", stage="")
            else:
                self._set_state(job, item, "error", message=clean_error(str(exc)), stage="")
                job.add_log("error", f"{item.title}: {clean_error(str(exc))}", item.uid)
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, never fatal
            if item.cancel.is_set() or job.cancel_all.is_set():
                self._set_state(job, item, "cancelled", message="Cancelled", stage="")
            else:
                self._set_state(job, item, "error", message=clean_error(str(exc)) or exc.__class__.__name__, stage="")
                job.add_log("error", f"{item.title}: {exc!r}", item.uid)
            return

        if item.cancel.is_set() or job.cancel_all.is_set():
            self._set_state(job, item, "cancelled", message="Cancelled", stage="")
            return

        final_path = extract_final_path(info) or item.filepath
        if info is None:
            self._set_state(job, item, "error", message="yt-dlp returned no result", stage="")
            return
        requested = info.get("requested_downloads") if isinstance(info, dict) else None
        if not requested and archive_path is not None:
            self._set_state(job, item, "skipped", message="Already in the download archive", stage="")
            return
        if final_path:
            item.filepath = final_path
            job.written_files.append(final_path)
        self._set_state(job, item, "done", stage="", message=Path(final_path).name if final_path else "")


POSTPROCESSOR_LABELS = {
    "MetadataParser": "Reading tags",
    "SponsorBlock": "Checking SponsorBlock",
    "ModifyChapters": "Trimming non-music",
    "ExtractAudio": "Converting audio",
    "FFmpegExtractAudio": "Converting audio",
    "Metadata": "Writing tags",
    "FFmpegMetadata": "Writing tags",
    "EmbedThumbnail": "Embedding cover art",
    "FFmpegVideoConvertor": "Converting video",
    "Merger": "Merging streams",
    "FFmpegEmbedSubtitle": "Embedding subtitles",
    "MoveFiles": "Moving file",
}

_ERROR_NOISE = re.compile(r"^\s*ERROR:\s*", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean_error(message: str) -> str:
    text = _ANSI.sub("", message or "").strip()
    text = _ERROR_NOISE.sub("", text)
    text = re.sub(r"\s*;\s*please report this issue on\s+https\S+.*$", "", text, flags=re.I | re.S)
    return text.strip()[:400]


def extract_final_path(info: Any) -> str:
    """Dig the real on-disk path out of a completed yt-dlp info dict."""
    if not isinstance(info, dict):
        return ""
    requested = info.get("requested_downloads")
    if isinstance(requested, list) and requested:
        entry = requested[0]
        if isinstance(entry, dict):
            for key in ("filepath", "_filename", "filename"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    return value
    for key in ("filepath", "_filename"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


class AppState:
    def __init__(self, settings_store: SettingsStore, token: str, port: int) -> None:
        self.settings = settings_store
        self.jobs = JobManager(settings_store)
        self.token = token
        self.port = port
        self.started = time.time()
        self.server: Optional[ThreadingHTTPServer] = None


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ytmd/{APP_VERSION}"
    sys_version = ""
    state: AppState  # injected by make_server()

    # -- infrastructure ------------------------------------------------------ #

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        if os.environ.get("YTMD_HTTP_LOG"):
            log(f"{self.address_string()} {fmt % args}")

    def _host_ok(self) -> bool:
        """Pin the Host header to loopback names; this is what stops DNS rebinding."""
        host = self.headers.get("Host", "")
        if not host:
            return False
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host[: host.index("]") + 1]
        return hostname.lower() in LOOPBACK_HOSTNAMES

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser clients (curl, tests) still need the token
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"}:
            return False
        return (parsed.hostname or "").lower() in LOOPBACK_HOSTNAMES and parsed.port == self.state.port

    def _authed(self) -> bool:
        supplied = self.headers.get("X-Ytmd-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, self.state.token)

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Bad Content-Length header")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body is too large")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Request body was not valid JSON")
        return parsed if isinstance(parsed, dict) else {}

    # -- verbs ---------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Deliberately no Access-Control-Allow-* headers: no other origin may
        # talk to this service, so every preflight is refused.
        self._send(HTTPStatus.NO_CONTENT, b"", "text/plain", {"Allow": "GET, HEAD, POST, OPTIONS"})

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                self._json(403, {"error": "Requests must be addressed to localhost."})
                return
            path = urlparse(self.path).path
            if path.startswith("/api/"):
                if not self._origin_ok():
                    raise ApiError(403, "Cross-origin requests are not accepted.")
                if not self._authed():
                    raise ApiError(401, "Missing or invalid session token. Reload the page.")
                self._route_api(method, path)
                return
            self._route_static(path)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001 - never let one request kill the thread
            log(traceback.format_exc())
            try:
                self._json(500, {"error": "Internal error. See the terminal running ytmd for details."})
            except Exception:
                pass

    # -- static --------------------------------------------------------------- #

    def _route_static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            try:
                html = INDEX_HTML.read_text("utf-8")
            except OSError:
                self._send(500, b"index.html is missing next to ytmd.py", "text/plain; charset=utf-8")
                return
            html = html.replace("__YTMD_TOKEN__", self.state.token).replace("__YTMD_VERSION__", APP_VERSION)
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                # The UI is entirely self-contained apart from YouTube thumbnails.
                {
                    "Content-Security-Policy": (
                        "default-src 'none'; img-src 'self' data: https://i.ytimg.com https://*.ggpht.com "
                        "https://yt3.ggpht.com https://lh3.googleusercontent.com; "
                        "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                        "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
                    )
                },
            )
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    # -- api ------------------------------------------------------------------- #

    def _route_api(self, method: str, path: str) -> None:
        state = self.state

        if path == "/api/health" and method == "GET":
            ffmpeg_dir = find_ffmpeg(state.settings.get().get("ffmpegLocation", ""))
            self._json(
                200,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "version": APP_VERSION,
                    "ytdlp": yt_dlp.version.__version__,
                    "python": platform.python_version(),
                    "platform": sys.platform,
                    "ffmpeg": bool(ffmpeg_dir),
                    "ffmpegDir": ffmpeg_dir or "",
                    "metadataParser": _TITLE_INTERPRETER is not None,
                    "jsRuntimes": detect_js_runtimes(),
                    "jsRuntime": chosen_js_runtime(state.settings.get()),
                    "uptime": round(time.time() - state.started, 1),
                },
            )
            return

        if path == "/api/settings":
            if method == "GET":
                self._json(200, {"settings": state.settings.get(), "defaults": DEFAULT_SETTINGS})
                return
            body = self._read_json()
            self._json(200, {"settings": state.settings.update(body.get("settings", body))})
            return

        if path == "/api/resolve" and method == "POST":
            body = self._read_json()
            url = str(body.get("url") or "").strip()
            if not url:
                raise ApiError(400, "No URL was provided.")
            settings = coerce_settings(body.get("settings"), state.settings.get())
            try:
                self._json(200, resolve_source(url, settings))
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except DownloadError as exc:
                raise ApiError(502, clean_error(str(exc)) or "yt-dlp could not read that link.")
            return

        if path == "/api/path/check" and method == "POST":
            body = self._read_json()
            raw = str(body.get("path") or "").strip()
            self._json(200, inspect_path(raw))
            return

        if path == "/api/jobs" and method == "POST":
            body = self._read_json()
            tracks = body.get("tracks")
            if not isinstance(tracks, list) or not tracks:
                raise ApiError(400, "Select at least one track first.")
            if len(tracks) > MAX_JOB_ITEMS:
                raise ApiError(400, f"That is more than {MAX_JOB_ITEMS} tracks in one go.")
            cleaned: list[dict[str, Any]] = []
            for entry in tracks:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or "").strip()
                if not url.lower().startswith(("http://", "https://")):
                    continue
                cleaned.append(entry)
            if not cleaned:
                raise ApiError(400, "None of the selected tracks had a usable URL.")
            settings = state.settings.update(body.get("settings", {}))
            job = state.jobs.create(cleaned, settings, str(body.get("playlistTitle") or ""))
            self._json(201, {"jobId": job.id, "job": job.snapshot()})
            return

        job_match = re.fullmatch(r"/api/jobs/([0-9a-f]{6,32})(/.*)?", path)
        if job_match:
            job = state.jobs.get(job_match.group(1))
            if job is None:
                raise ApiError(404, "That download job is no longer available.")
            tail = job_match.group(2) or ""
            if tail == "" and method == "GET":
                self._json(200, job.snapshot())
                return
            if tail == "/events" and method == "GET":
                self._stream_events(job)
                return
            if tail == "/cancel" and method == "POST":
                state.jobs.cancel(job.id)
                self._json(200, {"ok": True})
                return
            item_match = re.fullmatch(r"/items/([^/]{1,80})/cancel", tail)
            if item_match and method == "POST":
                ok = state.jobs.cancel_item(job.id, item_match.group(1))
                self._json(200, {"ok": ok})
                return
            raise ApiError(404, "Unknown job endpoint.")

        if path == "/api/reveal" and method == "POST":
            body = self._read_json()
            self._json(200, reveal_in_file_manager(str(body.get("path") or "")))
            return

        if path == "/api/quit" and method == "POST":
            self._json(200, {"ok": True})
            log("shutdown requested from the app")
            if state.server is not None:
                threading.Thread(target=state.server.shutdown, daemon=True).start()
            return

        raise ApiError(404, "Unknown endpoint.")

    # -- server-sent events ------------------------------------------------------ #

    def _stream_events(self, job: Job) -> None:
        listener = job.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            self._sse(job.snapshot())
            if job.state != "running":
                self._sse({"type": "end"})
                return

            last_beat = time.time()
            while True:
                try:
                    event = listener.get(timeout=1.0)
                except queue.Empty:
                    if time.time() - last_beat >= SSE_HEARTBEAT_SECONDS:
                        last_beat = time.time()
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    if job.state != "running":
                        self._sse({"type": "end"})
                        return
                    continue
                if event is None:
                    return
                self._sse(event)
                last_beat = time.time()
                if event.get("type") == "end":
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            job.unsubscribe(listener)

    def _sse(self, payload: dict[str, Any]) -> None:
        if "type" not in payload:
            payload = {"type": "snapshot", **payload}
        data = json.dumps(payload, default=str)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


def inspect_path(raw: str) -> dict[str, Any]:
    """Report whether an output folder is usable, without creating anything."""
    if not raw.strip():
        raw = default_output_dir()
    path = Path(raw).expanduser()
    result: dict[str, Any] = {"path": str(path), "exists": path.exists(), "writable": False, "free": None, "error": ""}
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        result["writable"] = os.access(probe, os.W_OK | os.X_OK)
        usage = shutil.disk_usage(probe)
        result["free"] = usage.free
    except OSError as exc:
        result["error"] = str(exc)
    if not result["writable"] and not result["error"]:
        result["error"] = f"No write permission for {probe}"
    return result


def reveal_in_file_manager(raw: str) -> dict[str, Any]:
    path = Path(raw or default_output_dir()).expanduser()
    if not path.exists():
        return {"ok": False, "error": f"{path} does not exist yet."}
    target = str(path if path.is_dir() else path.parent)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", target])
        elif sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as exc:  # noqa: BLE001 - purely cosmetic feature
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": target}


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


def make_server(host: str, port: int, state: AppState) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    state.server = server
    state.port = server.server_address[1]
    return server


BANNER = r"""
                     _
   _  _| |_ _ _  _| |
  | || |  _| ' \/ _` |    {app}
   \_, |\__|_|_|\__,_|    v{version}
   |__/
"""


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ytmd",
        description=f"{APP_NAME} - local web app backed by yt-dlp.",
    )
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765, 0 picks a free one)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address; loopback only (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window on start")
    parser.add_argument("--output", default="", help="override the download folder for this run")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION} (yt-dlp {yt_dlp.version.__version__})")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        log(
            f"refusing to bind {args.host}: this service drives yt-dlp with whatever "
            "options the page sends, so it is loopback-only by design."
        )
        return 2

    settings_store = SettingsStore()
    if args.output:
        settings_store.update({"outputDir": args.output})

    token = secrets.token_urlsafe(32)
    state = AppState(settings_store, token, args.port)

    try:
        server = make_server(args.host, args.port, state)
    except OSError as exc:
        log(f"could not bind {args.host}:{args.port} - {exc}")
        log("try a different port:  python3 ytmd.py --port 8899")
        return 1

    url = f"http://127.0.0.1:{state.port}/"
    ffmpeg_dir = find_ffmpeg(settings_store.get().get("ffmpegLocation", ""))

    print(BANNER.format(app=APP_NAME, version=APP_VERSION))
    print(f"  yt-dlp    {yt_dlp.version.__version__}")
    print(f"  ffmpeg    {ffmpeg_dir or 'NOT FOUND - audio conversion and tagging are disabled'}")
    runtime = chosen_js_runtime(settings_store.get())
    runtimes = detect_js_runtimes()
    print(f"  js        {runtime + ' ' + runtimes.get(runtime, '') if runtime else 'NOT FOUND - YouTube extraction will likely fail'}")
    print(f"  saving to {settings_store.get().get('outputDir')}")
    print(f"  settings  {settings_store.path}")
    print()
    print(f"  Open  ->  {url}")
    print("  Press Ctrl+C to stop.")
    print()

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n  Stopping...")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
