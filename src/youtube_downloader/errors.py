"""Exception types raised by the YouTube downloader."""

from __future__ import annotations


class YouTubeDownloaderError(Exception):
    """Base class for every error raised by this package."""


class InvalidURLError(YouTubeDownloaderError):
    """The supplied string is not a URL this downloader will accept."""


class MetadataError(YouTubeDownloaderError):
    """Metadata for a video could not be retrieved."""


class DownloadFailedError(YouTubeDownloaderError):
    """yt-dlp reported a failure while downloading."""


class FFmpegMissingError(YouTubeDownloaderError):
    """An ffmpeg binary is required for the requested options but was not found."""
