"""A typed wrapper around yt-dlp for fetching YouTube videos, audio and metadata."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urlparse

from .errors import (
    DownloadFailedError,
    FFmpegMissingError,
    InvalidURLError,
    MetadataError,
)
from .options import DownloadOptions, build_ydl_options

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)


def is_youtube_url(url: str) -> bool:
    """Whether ``url`` is an ``http(s)`` URL served by a known YouTube host."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in YOUTUBE_HOSTS


def validate_url(url: str, *, allow_other_sites: bool = False) -> str:
    """Return the trimmed URL, raising :class:`InvalidURLError` if unusable."""
    candidate = url.strip()
    if not candidate:
        raise InvalidURLError("empty URL")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InvalidURLError(f"not an http(s) URL: {url!r}")
    if not allow_other_sites and not is_youtube_url(candidate):
        raise InvalidURLError(
            f"{parsed.netloc} is not a YouTube host; pass allow_other_sites=True "
            "to download from any site yt-dlp supports"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class Progress:
    """One progress report for a single file."""

    status: str
    filename: str | None
    downloaded_bytes: int | None
    total_bytes: int | None
    speed: float | None
    eta: int | None

    @property
    def percent(self) -> float | None:
        """Completion in the range 0-100, or ``None`` when the size is unknown."""
        if not self.total_bytes or self.downloaded_bytes is None:
            return None
        return min(100.0, self.downloaded_bytes * 100.0 / self.total_bytes)


ProgressCallback = Callable[[Progress], None]


@dataclass(frozen=True, slots=True)
class Format:
    """One downloadable stream reported by yt-dlp."""

    format_id: str
    ext: str
    resolution: str | None
    fps: float | None
    vcodec: str | None
    acodec: str | None
    filesize: int | None
    note: str | None

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec) and self.vcodec != "none"

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec) and self.acodec != "none"


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """Metadata for a single video."""

    video_id: str
    title: str
    url: str
    uploader: str | None
    duration: float | None
    upload_date: str | None
    view_count: int | None
    description: str | None
    thumbnail: str | None
    formats: tuple[Format, ...] = ()

    @property
    def duration_str(self) -> str:
        """Duration as ``H:MM:SS`` (or ``M:SS``), ``"unknown"`` when absent."""
        if self.duration is None:
            return "unknown"
        total = int(self.duration)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Where a single downloaded video ended up."""

    video_id: str
    title: str
    path: Path | None
    filesize: int | None

    @property
    def succeeded(self) -> bool:
        """Whether a file for this entry is on disk."""
        return self.path is not None and self.path.exists()

    @property
    def skipped(self) -> bool:
        """Whether yt-dlp deliberately wrote nothing for this entry.

        A real download failure raises instead, so an entry that comes back
        without a filename was skipped: already in the download archive,
        already present on disk, or filtered out.
        """
        return self.path is None

    @property
    def failed(self) -> bool:
        """Whether a file was expected but is not there."""
        return self.path is not None and not self.path.exists()


def _as_format(raw: dict[str, Any]) -> Format:
    return Format(
        format_id=str(raw.get("format_id", "")),
        ext=str(raw.get("ext", "")),
        resolution=raw.get("resolution") or _resolution_from(raw),
        fps=raw.get("fps"),
        vcodec=raw.get("vcodec"),
        acodec=raw.get("acodec"),
        filesize=raw.get("filesize") or raw.get("filesize_approx"),
        note=raw.get("format_note"),
    )


def _resolution_from(raw: dict[str, Any]) -> str | None:
    width, height = raw.get("width"), raw.get("height")
    if width and height:
        return f"{width}x{height}"
    if height:
        return f"{height}p"
    return None


def _as_video_info(raw: dict[str, Any]) -> VideoInfo:
    formats = tuple(_as_format(f) for f in raw.get("formats") or () if isinstance(f, dict))
    return VideoInfo(
        video_id=str(raw.get("id", "")),
        title=str(raw.get("title", "")),
        url=str(raw.get("webpage_url") or raw.get("original_url") or ""),
        uploader=raw.get("uploader") or raw.get("channel"),
        duration=raw.get("duration"),
        upload_date=raw.get("upload_date"),
        view_count=raw.get("view_count"),
        description=raw.get("description"),
        thumbnail=raw.get("thumbnail"),
        formats=formats,
    )


def _iter_entries(info: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield leaf video entries, flattening playlists (and nested playlists)."""
    entries = info.get("entries")
    if entries is None:
        yield info
        return
    for entry in entries:
        if isinstance(entry, dict):
            yield from _iter_entries(entry)


def _result_from_entry(entry: dict[str, Any]) -> DownloadResult:
    path: Path | None = None
    requested = entry.get("requested_downloads") or ()
    for item in requested:
        filepath = item.get("filepath") or item.get("_filename")
        if filepath:
            path = Path(filepath)
            break
    if path is None and entry.get("filepath"):
        path = Path(entry["filepath"])
    filesize = entry.get("filesize") or entry.get("filesize_approx")
    if filesize is None and path is not None and path.exists():
        filesize = path.stat().st_size
    return DownloadResult(
        video_id=str(entry.get("id", "")),
        title=str(entry.get("title", "")),
        path=path,
        filesize=filesize,
    )


def ffmpeg_available() -> bool:
    """Whether an ffmpeg binary is on ``PATH``."""
    return shutil.which("ffmpeg") is not None


class YouTubeDownloader:
    """Download videos, extract audio, and read metadata from YouTube.

    The instance holds the settings; each method takes the URLs to act on::

        downloader = YouTubeDownloader(DownloadOptions(quality="1080p"))
        results = downloader.download("https://youtu.be/dQw4w9WgXcQ")
    """

    def __init__(
        self,
        options: DownloadOptions | None = None,
        *,
        allow_other_sites: bool = False,
    ) -> None:
        self.options = options or DownloadOptions()
        self.allow_other_sites = allow_other_sites

    # -- public API ----------------------------------------------------

    def probe(self, url: str) -> VideoInfo:
        """Fetch metadata for ``url`` without downloading anything."""
        raw = self._extract(url, download=False)
        entries = list(_iter_entries(raw))
        if not entries:
            raise MetadataError(f"no video found at {url}")
        return _as_video_info(entries[0])

    def list_formats(self, url: str) -> list[Format]:
        """List the streams available for ``url``, best last."""
        return list(self.probe(url).formats)

    def download(
        self,
        urls: str | Sequence[str],
        *,
        progress: ProgressCallback | None = None,
    ) -> list[DownloadResult]:
        """Download every URL, returning one result per video.

        Playlist URLs expand to one result per entry when
        :attr:`DownloadOptions.playlist` is set.
        """
        targets = [urls] if isinstance(urls, str) else list(urls)
        if not targets:
            return []
        options = self._resolve_ffmpeg(self.options)
        Path(options.output_dir).mkdir(parents=True, exist_ok=True)

        results: list[DownloadResult] = []
        for url in targets:
            raw = self._extract(
                url, download=True, options=options, progress=progress
            )
            results.extend(_result_from_entry(entry) for entry in _iter_entries(raw))
        return results

    def download_audio(
        self,
        urls: str | Sequence[str],
        *,
        audio_format: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[DownloadResult]:
        """Download ``urls`` and keep only the audio track."""
        options = replace(
            self.options,
            audio_only=True,
            audio_format=audio_format or self.options.audio_format,
        )
        downloader = YouTubeDownloader(options, allow_other_sites=self.allow_other_sites)
        return downloader.download(urls, progress=progress)

    # -- internals -----------------------------------------------------

    def _resolve_ffmpeg(self, options: DownloadOptions) -> DownloadOptions:
        """Degrade or reject options that need an ffmpeg binary we do not have."""
        if ffmpeg_available():
            return options
        if options.requires_ffmpeg:
            raise FFmpegMissingError(
                "ffmpeg is required for audio extraction, format conversion, and "
                "embedding subtitles or thumbnails. Install it and try again."
            )
        logger.warning(
            "ffmpeg not found; falling back to the best single-file stream, "
            "which may be lower quality than the separate video and audio streams"
        )
        return options.degraded_without_ffmpeg()

    def _extract(
        self,
        url: str,
        *,
        download: bool,
        options: DownloadOptions | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        import yt_dlp

        target = validate_url(url, allow_other_sites=self.allow_other_sites)
        settings = options or self.options
        ydl_options = build_ydl_options(settings, download=download)
        if settings.quiet:
            ydl_options.setdefault("logger", _QuietLogger())
        if progress is not None:
            ydl_options["progress_hooks"] = [_progress_hook(progress)]

        try:
            with yt_dlp.YoutubeDL(ydl_options) as ydl:
                raw = ydl.extract_info(target, download=download)
        except yt_dlp.utils.DownloadError as exc:
            error = MetadataError if not download else DownloadFailedError
            raise error(f"{target}: {exc}") from exc
        if raw is None:
            error = MetadataError if not download else DownloadFailedError
            raise error(f"{target}: yt-dlp returned no information")
        return raw


class _QuietLogger:
    """Routes yt-dlp's own output into this module's logger.

    yt-dlp prints errors to stderr even when told to be quiet, which would
    duplicate the exception this package raises for the same failure.
    """

    def debug(self, message: str) -> None:
        logger.debug(message)

    info = debug
    warning = debug

    def error(self, message: str) -> None:
        # The caller sees this as a raised exception carrying the same text.
        logger.debug(message)


def _progress_hook(callback: ProgressCallback) -> Callable[[dict[str, Any]], None]:
    def hook(payload: dict[str, Any]) -> None:
        callback(
            Progress(
                status=str(payload.get("status", "")),
                filename=payload.get("filename"),
                downloaded_bytes=payload.get("downloaded_bytes"),
                total_bytes=payload.get("total_bytes") or payload.get("total_bytes_estimate"),
                speed=payload.get("speed"),
                eta=payload.get("eta"),
            )
        )

    return hook
