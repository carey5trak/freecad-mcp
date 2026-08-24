"""A stand-in for yt-dlp, so the suite needs no network and downloads nothing."""

from __future__ import annotations

import sys
import types
from typing import Any

VIDEO_INFO: dict[str, Any] = {
    "id": "abc123",
    "title": "Example Video",
    "webpage_url": "https://www.youtube.com/watch?v=abc123",
    "uploader": "Example Channel",
    "duration": 3725.0,
    "upload_date": "20250101",
    "view_count": 42,
    "description": "A description.",
    "thumbnail": "https://img.example/abc123.jpg",
    "formats": [
        {"format_id": "18", "ext": "mp4", "width": 640, "height": 360, "fps": 30,
         "vcodec": "avc1", "acodec": "mp4a", "filesize": 1048576, "format_note": "360p"},
        {"format_id": "137", "ext": "mp4", "width": 1920, "height": 1080, "fps": 30,
         "vcodec": "avc1", "acodec": "none", "filesize_approx": 10485760},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "filesize": 524288},
    ],
}


class FakeDownloadError(Exception):
    """Stands in for ``yt_dlp.utils.DownloadError``."""


class FakeYoutubeDL:
    """Stands in for ``yt_dlp.YoutubeDL``, recording the options it was given."""

    instances: list["FakeYoutubeDL"] = []
    result: dict | None = None
    error: Exception | None = None
    #: Progress events replayed through the caller's hooks during a download.
    progress_events: list[dict] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.calls: list[tuple[str, bool]] = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict | None:
        self.calls.append((url, download))
        if FakeYoutubeDL.error is not None:
            raise FakeYoutubeDL.error
        if download:
            for hook in self.options.get("progress_hooks") or ():
                for event in FakeYoutubeDL.progress_events:
                    hook(event)
        return FakeYoutubeDL.result


def install(monkeypatch) -> type[FakeYoutubeDL]:
    """Swap the real yt-dlp out for :class:`FakeYoutubeDL` and reset its state."""
    from youtube_downloader import downloader as downloader_module

    FakeYoutubeDL.instances = []
    FakeYoutubeDL.result = VIDEO_INFO
    FakeYoutubeDL.error = None
    FakeYoutubeDL.progress_events = []

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL
    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadError = FakeDownloadError
    module.utils = utils
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils)
    monkeypatch.setattr(downloader_module, "ffmpeg_available", lambda: True)
    return FakeYoutubeDL
