"""Download YouTube videos, audio and metadata through a small typed API.

    from youtube_downloader import DownloadOptions, YouTubeDownloader

    downloader = YouTubeDownloader(DownloadOptions(quality="1080p"))
    for result in downloader.download("https://youtu.be/dQw4w9WgXcQ"):
        print(result.path)
"""

from .downloader import (
    DownloadResult,
    Format,
    Progress,
    ProgressCallback,
    VideoInfo,
    YouTubeDownloader,
    ffmpeg_available,
    is_youtube_url,
    validate_url,
)
from .errors import (
    DownloadFailedError,
    FFmpegMissingError,
    InvalidURLError,
    MetadataError,
    YouTubeDownloaderError,
)
from .options import (
    DEFAULT_TEMPLATE,
    DownloadOptions,
    build_ydl_options,
    format_selector,
    parse_rate_limit,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "DownloadFailedError",
    "DownloadOptions",
    "DownloadResult",
    "FFmpegMissingError",
    "Format",
    "InvalidURLError",
    "MetadataError",
    "Progress",
    "ProgressCallback",
    "VideoInfo",
    "YouTubeDownloader",
    "YouTubeDownloaderError",
    "build_ydl_options",
    "ffmpeg_available",
    "format_selector",
    "is_youtube_url",
    "parse_rate_limit",
    "validate_url",
]
