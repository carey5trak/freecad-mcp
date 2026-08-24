"""User-facing download settings and their translation into yt-dlp options."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


DEFAULT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"

#: Quality aliases understood by :func:`format_selector`, in addition to any
#: bare height such as ``1080`` or ``1080p``.
QUALITY_ALIASES = frozenset({"best", "worst"})

_HEIGHT_RE = re.compile(r"\A(\d{3,4})p?\Z")
_RATE_RE = re.compile(r"\A(\d+(?:\.\d+)?)\s*([kmg]?)b?\Z", re.IGNORECASE)
_RATE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def format_selector(quality: str) -> str:
    """Translate a quality string into a yt-dlp format selector.

    ``best`` and ``worst`` map onto the matching merged streams; anything else
    is read as a maximum height (``720``/``720p``), which keeps the aspect
    ratio free so vertical videos are not accidentally excluded.
    """
    value = quality.strip().lower()
    if value == "best":
        return "bestvideo*+bestaudio/best"
    if value == "worst":
        return "worstvideo*+worstaudio/worst"
    match = _HEIGHT_RE.match(value)
    if match is None:
        raise ValueError(
            f"unknown quality {quality!r}: use 'best', 'worst', or a height such as '1080p'"
        )
    height = int(match.group(1))
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"


def parse_rate_limit(rate: str) -> int:
    """Parse a human rate limit (``500K``, ``2M``, ``1048576``) into bytes/second."""
    match = _RATE_RE.match(rate.strip())
    if match is None:
        raise ValueError(f"unknown rate limit {rate!r}: use forms like '500K', '2M', '1048576'")
    amount, unit = match.groups()
    value = int(float(amount) * _RATE_UNITS[unit.lower()])
    if value <= 0:
        raise ValueError(f"rate limit must be positive, got {rate!r}")
    return value


@dataclass(frozen=True, slots=True)
class DownloadOptions:
    """Everything that shapes a download, independent of yt-dlp's own naming.

    The defaults download the best available video+audio for a single video
    into ``./downloads`` without touching the network beyond that.
    """

    output_dir: Path = Path("downloads")
    filename_template: str = DEFAULT_TEMPLATE
    quality: str = "best"
    format_spec: str | None = None
    container: str | None = None
    audio_only: bool = False
    audio_format: str = "mp3"
    audio_bitrate: str = "192"
    subtitles: bool = False
    subtitle_langs: tuple[str, ...] = ("en",)
    auto_subtitles: bool = False
    embed_subtitles: bool = False
    embed_metadata: bool = True
    embed_thumbnail: bool = False
    write_thumbnail: bool = False
    playlist: bool = False
    playlist_items: str | None = None
    archive_file: Path | None = None
    cookies_file: Path | None = None
    cookies_from_browser: str | None = None
    rate_limit: str | None = None
    retries: int = 3
    concurrent_fragments: int = 1
    overwrite: bool = False
    quiet: bool = True
    extra_ydl_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.format_spec is None:
            # Fail fast on a bad quality string rather than deep inside yt-dlp.
            format_selector(self.quality)
        if self.rate_limit is not None:
            parse_rate_limit(self.rate_limit)
        if self.retries < 0:
            raise ValueError("retries must not be negative")
        if self.concurrent_fragments < 1:
            raise ValueError("concurrent_fragments must be at least 1")

    @property
    def requires_ffmpeg(self) -> bool:
        """Whether these options simply cannot be honoured without ffmpeg."""
        return (
            self.audio_only
            or self.embed_subtitles
            or self.embed_thumbnail
            or self.container is not None
        )

    def degraded_without_ffmpeg(self) -> "DownloadOptions":
        """Return options that still work when no ffmpeg binary is present.

        Merging separate video and audio streams and writing container
        metadata both need ffmpeg, so fall back to the best progressive
        stream (a single file that already carries both) and drop the
        metadata post-processor. Callers must reject options for which
        :attr:`requires_ffmpeg` is true before calling this.
        """
        if self.requires_ffmpeg:
            raise ValueError("these options cannot be satisfied without ffmpeg")
        spec = self.format_spec
        if spec is None:
            spec = "best[vcodec!=none][acodec!=none]/best"
        return replace(self, format_spec=spec, embed_metadata=False)


def build_ydl_options(options: DownloadOptions, *, download: bool = True) -> dict[str, Any]:
    """Render :class:`DownloadOptions` into the dict yt-dlp expects.

    ``download=False`` produces a metadata-only configuration, used by
    ``probe`` and ``list_formats`` so they never write to disk.
    """
    ydl: dict[str, Any] = {
        # The full path goes in outtmpl alone; also setting "paths" would make
        # yt-dlp join the two and nest the output directory inside itself.
        "outtmpl": str(Path(options.output_dir) / options.filename_template),
        "noplaylist": not options.playlist,
        "quiet": options.quiet,
        "no_warnings": options.quiet,
        "noprogress": options.quiet,
        "retries": options.retries,
        "fragment_retries": options.retries,
        "ignoreerrors": False,
        "overwrites": options.overwrite,
        "continuedl": not options.overwrite,
        "concurrent_fragment_downloads": options.concurrent_fragments,
    }

    if options.audio_only:
        ydl["format"] = options.format_spec or "bestaudio/best"
    else:
        ydl["format"] = options.format_spec or format_selector(options.quality)
        if options.container:
            ydl["merge_output_format"] = options.container

    if not download:
        ydl["skip_download"] = True
        ydl["simulate"] = True
        # Metadata-only runs must not leave subtitle or thumbnail files behind.
        return ydl | options.extra_ydl_options

    postprocessors: list[dict[str, Any]] = []
    if options.audio_only:
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": options.audio_format,
                "preferredquality": options.audio_bitrate,
            }
        )
    if options.subtitles:
        ydl["writesubtitles"] = True
        ydl["writeautomaticsub"] = options.auto_subtitles
        ydl["subtitleslangs"] = list(options.subtitle_langs)
        if options.embed_subtitles and not options.audio_only:
            postprocessors.append({"key": "FFmpegEmbedSubtitle"})
    if options.write_thumbnail or options.embed_thumbnail:
        ydl["writethumbnail"] = True
        if options.embed_thumbnail:
            postprocessors.append({"key": "EmbedThumbnail"})
    if options.embed_metadata:
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
    if postprocessors:
        ydl["postprocessors"] = postprocessors

    if options.playlist_items:
        ydl["playlist_items"] = options.playlist_items
    if options.archive_file:
        ydl["download_archive"] = str(options.archive_file)
    if options.cookies_file:
        ydl["cookiefile"] = str(options.cookies_file)
    if options.cookies_from_browser:
        ydl["cookiesfrombrowser"] = (options.cookies_from_browser,)
    if options.rate_limit:
        ydl["ratelimit"] = parse_rate_limit(options.rate_limit)

    return ydl | options.extra_ydl_options
