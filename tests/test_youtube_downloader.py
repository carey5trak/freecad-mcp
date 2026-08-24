"""Tests for the youtube_downloader package. No network access required."""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from youtube_downloader import (
    DownloadFailedError,
    DownloadOptions,
    FFmpegMissingError,
    InvalidURLError,
    MetadataError,
    YouTubeDownloader,
    build_ydl_options,
    format_selector,
    is_youtube_url,
    parse_rate_limit,
    validate_url,
)
from youtube_downloader import cli, downloader as downloader_module


# -- URL handling ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "http://m.youtube.com/watch?v=abc",
        "https://music.youtube.com/watch?v=abc",
    ],
)
def test_is_youtube_url_accepts_known_hosts(url: str) -> None:
    assert is_youtube_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/12345",
        "ftp://youtube.com/watch?v=abc",
        "not a url",
        "",
        "https://notyoutube.com/watch?v=abc",
    ],
)
def test_is_youtube_url_rejects_everything_else(url: str) -> None:
    assert not is_youtube_url(url)


def test_validate_url_trims_and_returns() -> None:
    assert validate_url("  https://youtu.be/abc  ") == "https://youtu.be/abc"


def test_validate_url_rejects_non_youtube_by_default() -> None:
    with pytest.raises(InvalidURLError, match="not a YouTube host"):
        validate_url("https://vimeo.com/12345")


def test_validate_url_allows_other_sites_when_asked() -> None:
    assert validate_url("https://vimeo.com/12345", allow_other_sites=True)


def test_validate_url_rejects_non_http_scheme() -> None:
    with pytest.raises(InvalidURLError, match="not an http"):
        validate_url("file:///etc/passwd", allow_other_sites=True)


# -- quality and rate parsing -----------------------------------------


def test_format_selector_best_and_worst() -> None:
    assert format_selector("best") == "bestvideo*+bestaudio/best"
    assert format_selector("worst") == "worstvideo*+worstaudio/worst"


@pytest.mark.parametrize("quality", ["1080p", "1080", "1080P", " 1080p "])
def test_format_selector_accepts_height_spellings(quality: str) -> None:
    assert format_selector(quality) == (
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    )


@pytest.mark.parametrize("quality", ["hd", "4k", "", "10800p0"])
def test_format_selector_rejects_unknown_quality(quality: str) -> None:
    with pytest.raises(ValueError):
        format_selector(quality)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1048576", 1048576), ("500K", 512000), ("2M", 2097152), ("1.5M", 1572864)],
)
def test_parse_rate_limit(text: str, expected: int) -> None:
    assert parse_rate_limit(text) == expected


@pytest.mark.parametrize("text", ["fast", "-1M", "0", ""])
def test_parse_rate_limit_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_rate_limit(text)


# -- option validation -------------------------------------------------


def test_options_reject_bad_quality_eagerly() -> None:
    with pytest.raises(ValueError):
        DownloadOptions(quality="ultra")


def test_options_skip_quality_check_when_format_spec_given() -> None:
    options = DownloadOptions(quality="ultra", format_spec="bestaudio")
    assert options.format_spec == "bestaudio"


def test_options_reject_negative_retries() -> None:
    with pytest.raises(ValueError):
        DownloadOptions(retries=-1)


def test_options_reject_zero_concurrent_fragments() -> None:
    with pytest.raises(ValueError):
        DownloadOptions(concurrent_fragments=0)


# -- yt-dlp option building -------------------------------------------


def test_build_ydl_options_defaults(tmp_path: Path) -> None:
    ydl = build_ydl_options(DownloadOptions(output_dir=tmp_path))
    assert ydl["format"] == "bestvideo*+bestaudio/best"
    assert ydl["noplaylist"] is True
    assert ydl["outtmpl"].startswith(str(tmp_path))
    assert ydl["overwrites"] is False
    assert ydl["continuedl"] is True


def test_build_ydl_options_audio_adds_extract_postprocessor() -> None:
    ydl = build_ydl_options(DownloadOptions(audio_only=True, audio_format="flac"))
    assert ydl["format"] == "bestaudio/best"
    extract = [p for p in ydl["postprocessors"] if p["key"] == "FFmpegExtractAudio"]
    assert extract and extract[0]["preferredcodec"] == "flac"


def test_build_ydl_options_container_sets_merge_format() -> None:
    ydl = build_ydl_options(DownloadOptions(container="mkv"))
    assert ydl["merge_output_format"] == "mkv"


def test_build_ydl_options_subtitles() -> None:
    ydl = build_ydl_options(
        DownloadOptions(subtitles=True, subtitle_langs=("en", "ja"), embed_subtitles=True)
    )
    assert ydl["writesubtitles"] is True
    assert ydl["subtitleslangs"] == ["en", "ja"]
    assert any(p["key"] == "FFmpegEmbedSubtitle" for p in ydl["postprocessors"])


def test_build_ydl_options_metadata_only_skips_download() -> None:
    ydl = build_ydl_options(DownloadOptions(subtitles=True), download=False)
    assert ydl["skip_download"] is True
    assert ydl["simulate"] is True
    assert "postprocessors" not in ydl
    assert "writesubtitles" not in ydl


def test_build_ydl_options_playlist_and_archive(tmp_path: Path) -> None:
    archive = tmp_path / "seen.txt"
    ydl = build_ydl_options(
        DownloadOptions(playlist=True, playlist_items="1-3", archive_file=archive)
    )
    assert ydl["noplaylist"] is False
    assert ydl["playlist_items"] == "1-3"
    assert ydl["download_archive"] == str(archive)


def test_build_ydl_options_rate_limit_and_cookies(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    ydl = build_ydl_options(
        DownloadOptions(rate_limit="2M", cookies_file=cookies, cookies_from_browser="firefox")
    )
    assert ydl["ratelimit"] == 2097152
    assert ydl["cookiefile"] == str(cookies)
    assert ydl["cookiesfrombrowser"] == ("firefox",)


def test_extra_ydl_options_win() -> None:
    ydl = build_ydl_options(DownloadOptions(extra_ydl_options={"format": "18", "geo_bypass": True}))
    assert ydl["format"] == "18"
    assert ydl["geo_bypass"] is True


# -- ffmpeg handling ---------------------------------------------------


def test_requires_ffmpeg_flags() -> None:
    assert DownloadOptions(audio_only=True).requires_ffmpeg
    assert DownloadOptions(container="mp4").requires_ffmpeg
    assert DownloadOptions(embed_thumbnail=True).requires_ffmpeg
    assert not DownloadOptions().requires_ffmpeg


def test_degraded_options_use_a_progressive_stream() -> None:
    degraded = DownloadOptions().degraded_without_ffmpeg()
    assert degraded.format_spec == "best[vcodec!=none][acodec!=none]/best"
    assert degraded.embed_metadata is False


def test_degraded_options_refuse_impossible_settings() -> None:
    with pytest.raises(ValueError):
        DownloadOptions(audio_only=True).degraded_without_ffmpeg()


def test_download_without_ffmpeg_raises_for_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader_module, "ffmpeg_available", lambda: False)
    downloader = YouTubeDownloader(DownloadOptions(audio_only=True))
    with pytest.raises(FFmpegMissingError):
        downloader.download("https://youtu.be/abc")


# -- fake yt-dlp integration ------------------------------------------


VIDEO_INFO = {
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


class FakeYoutubeDL:
    """Stands in for ``yt_dlp.YoutubeDL``, recording the options it was given."""

    instances: list["FakeYoutubeDL"] = []
    result: dict | None = None
    error: Exception | None = None

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
        return FakeYoutubeDL.result


class FakeDownloadError(Exception):
    pass


@pytest.fixture
def fake_yt_dlp(monkeypatch: pytest.MonkeyPatch):
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.result = VIDEO_INFO
    FakeYoutubeDL.error = None
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL
    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadError = FakeDownloadError
    module.utils = utils
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils)
    monkeypatch.setattr(downloader_module, "ffmpeg_available", lambda: True)
    return FakeYoutubeDL


def test_probe_maps_metadata(fake_yt_dlp) -> None:
    info = YouTubeDownloader().probe("https://youtu.be/abc123")
    assert info.video_id == "abc123"
    assert info.title == "Example Video"
    assert info.uploader == "Example Channel"
    assert info.duration_str == "1:02:05"
    assert len(info.formats) == 3
    assert fake_yt_dlp.instances[0].calls == [("https://youtu.be/abc123", False)]


def test_probe_does_not_download(fake_yt_dlp) -> None:
    YouTubeDownloader().probe("https://youtu.be/abc123")
    assert fake_yt_dlp.instances[0].options["skip_download"] is True


def test_duration_str_without_hours() -> None:
    from youtube_downloader import VideoInfo

    info = VideoInfo("i", "t", "u", None, 125.0, None, None, None, None)
    assert info.duration_str == "2:05"
    assert replace(info, duration=None).duration_str == "unknown"


def test_list_formats_reports_stream_kinds(fake_yt_dlp) -> None:
    formats = YouTubeDownloader().list_formats("https://youtu.be/abc123")
    by_id = {f.format_id: f for f in formats}
    assert by_id["18"].has_video and by_id["18"].has_audio
    assert by_id["137"].has_video and not by_id["137"].has_audio
    assert by_id["140"].has_audio and not by_id["140"].has_video
    assert by_id["137"].filesize == 10485760
    assert by_id["137"].resolution == "1920x1080"


def test_download_returns_result_with_path(fake_yt_dlp, tmp_path: Path) -> None:
    target = tmp_path / "Example Video [abc123].mp4"
    target.write_bytes(b"video data")
    fake_yt_dlp.result = VIDEO_INFO | {"requested_downloads": [{"filepath": str(target)}]}

    results = YouTubeDownloader(DownloadOptions(output_dir=tmp_path)).download(
        "https://youtu.be/abc123"
    )
    assert len(results) == 1
    assert results[0].path == target
    assert results[0].filesize == len(b"video data")
    assert results[0].succeeded


def test_download_creates_output_directory(fake_yt_dlp, tmp_path: Path) -> None:
    output = tmp_path / "nested" / "dir"
    YouTubeDownloader(DownloadOptions(output_dir=output)).download("https://youtu.be/abc123")
    assert output.is_dir()


def test_download_flattens_playlist_entries(fake_yt_dlp, tmp_path: Path) -> None:
    fake_yt_dlp.result = {
        "entries": [
            {"id": "one", "title": "One"},
            {"entries": [{"id": "two", "title": "Two"}]},
            None,
        ]
    }
    results = YouTubeDownloader(
        DownloadOptions(output_dir=tmp_path, playlist=True)
    ).download("https://www.youtube.com/playlist?list=PL123")
    assert [r.video_id for r in results] == ["one", "two"]
    assert not any(r.succeeded for r in results)


def test_download_accepts_several_urls(fake_yt_dlp, tmp_path: Path) -> None:
    results = YouTubeDownloader(DownloadOptions(output_dir=tmp_path)).download(
        ["https://youtu.be/a", "https://youtu.be/b"]
    )
    assert len(results) == 2
    assert len(fake_yt_dlp.instances) == 2


def test_download_with_no_urls_is_a_noop(fake_yt_dlp) -> None:
    assert YouTubeDownloader().download([]) == []
    assert fake_yt_dlp.instances == []


def test_download_audio_sets_audio_options(fake_yt_dlp, tmp_path: Path) -> None:
    YouTubeDownloader(DownloadOptions(output_dir=tmp_path)).download_audio(
        "https://youtu.be/abc123", audio_format="opus"
    )
    options = fake_yt_dlp.instances[0].options
    assert options["format"] == "bestaudio/best"
    extract = [p for p in options["postprocessors"] if p["key"] == "FFmpegExtractAudio"]
    assert extract[0]["preferredcodec"] == "opus"


def test_download_error_becomes_download_failed(fake_yt_dlp, tmp_path: Path) -> None:
    fake_yt_dlp.error = FakeDownloadError("video unavailable")
    with pytest.raises(DownloadFailedError, match="video unavailable"):
        YouTubeDownloader(DownloadOptions(output_dir=tmp_path)).download("https://youtu.be/abc")


def test_probe_error_becomes_metadata_error(fake_yt_dlp) -> None:
    fake_yt_dlp.error = FakeDownloadError("private video")
    with pytest.raises(MetadataError, match="private video"):
        YouTubeDownloader().probe("https://youtu.be/abc")


def test_empty_extraction_raises(fake_yt_dlp) -> None:
    fake_yt_dlp.result = None
    with pytest.raises(MetadataError, match="no information"):
        YouTubeDownloader().probe("https://youtu.be/abc")


def test_progress_callback_is_forwarded(fake_yt_dlp, tmp_path: Path) -> None:
    seen = []
    YouTubeDownloader(DownloadOptions(output_dir=tmp_path)).download(
        "https://youtu.be/abc123", progress=seen.append
    )
    hook = fake_yt_dlp.instances[0].options["progress_hooks"][0]
    hook({"status": "downloading", "filename": "a.mp4", "downloaded_bytes": 50,
          "total_bytes": 200, "speed": 1024.0, "eta": 3})
    assert len(seen) == 1
    assert seen[0].percent == 25.0
    assert seen[0].filename == "a.mp4"


def test_progress_percent_unknown_without_total() -> None:
    from youtube_downloader import Progress

    assert Progress("downloading", "a", 10, None, None, None).percent is None


# -- CLI ---------------------------------------------------------------


def test_cli_info_prints_summary(fake_yt_dlp, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["info", "https://youtu.be/abc123"]) == 0
    out = capsys.readouterr().out
    assert "Example Video" in out
    assert "1:02:05" in out


def test_cli_info_json(fake_yt_dlp, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert cli.main(["info", "--json", "https://youtu.be/abc123"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "abc123"
    assert payload["uploader"] == "Example Channel"


def test_cli_formats_lists_streams(fake_yt_dlp, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["formats", "https://youtu.be/abc123"]) == 0
    out = capsys.readouterr().out
    assert "137" in out and "1920x1080" in out


def test_cli_download_passes_options(fake_yt_dlp, tmp_path: Path) -> None:
    code = cli.main(
        ["download", "https://youtu.be/abc123", "-o", str(tmp_path),
         "--quality", "720p", "--container", "mkv", "--rate-limit", "500K", "-q"]
    )
    options = fake_yt_dlp.instances[0].options
    assert options["format"] == "bestvideo[height<=720]+bestaudio/best[height<=720]"
    assert options["merge_output_format"] == "mkv"
    assert options["ratelimit"] == 512000
    assert code == 0  # the fake writes nothing, which counts as skipped


def test_cli_audio_command(fake_yt_dlp, tmp_path: Path) -> None:
    cli.main(["audio", "https://youtu.be/abc123", "-o", str(tmp_path), "--audio-format", "m4a", "-q"])
    options = fake_yt_dlp.instances[0].options
    assert options["format"] == "bestaudio/best"
    assert options["postprocessors"][0]["preferredcodec"] == "m4a"


def test_cli_reports_success_exit_code(fake_yt_dlp, tmp_path: Path) -> None:
    target = tmp_path / "out.mp4"
    target.write_bytes(b"x")
    fake_yt_dlp.result = VIDEO_INFO | {"requested_downloads": [{"filepath": str(target)}]}
    assert cli.main(["download", "https://youtu.be/abc123", "-o", str(tmp_path), "-q"]) == 0


def test_cli_rejects_non_youtube_url(fake_yt_dlp, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["info", "https://vimeo.com/1"]) == 1
    assert "not a YouTube host" in capsys.readouterr().err


def test_cli_any_site_flag(fake_yt_dlp) -> None:
    assert cli.main(["info", "--any-site", "https://vimeo.com/1"]) == 0


def test_cli_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_human_size_formats() -> None:
    assert cli._human_size(None) == "-"
    assert cli._human_size(512) == "512B"
    assert cli._human_size(2048) == "2.0KiB"
    assert cli._human_size(5 * 1024**2) == "5.0MiB"


@pytest.mark.parametrize("command", ["download", "audio", "info", "formats"])
def test_cli_help_renders(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    # argparse %-formats help strings, so a literal '%(title)s' in the default
    # output template must stay escaped.
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--help"])
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_outtmpl_resolves_into_the_output_directory_exactly_once(tmp_path: Path) -> None:
    # Regression: setting both "outtmpl" and "paths" made yt-dlp join them, so
    # files landed in <output_dir>/<output_dir>/. Uses the real yt-dlp filename
    # logic, which is entirely offline.
    import yt_dlp

    options = build_ydl_options(DownloadOptions(output_dir=tmp_path))
    with yt_dlp.YoutubeDL(options | {"quiet": True}) as ydl:
        filename = Path(ydl.prepare_filename({"id": "abc123", "title": "Clip", "ext": "mp4"}))
    assert filename.parent == tmp_path
    assert filename.name == "Clip [abc123].mp4"


def test_quiet_downloads_silence_yt_dlp_own_output(fake_yt_dlp, tmp_path: Path) -> None:
    YouTubeDownloader(DownloadOptions(output_dir=tmp_path)).download("https://youtu.be/abc")
    assert "logger" in fake_yt_dlp.instances[0].options


def test_verbose_downloads_leave_yt_dlp_output_alone(fake_yt_dlp, tmp_path: Path) -> None:
    options = DownloadOptions(output_dir=tmp_path, quiet=False)
    YouTubeDownloader(options).download("https://youtu.be/abc")
    assert "logger" not in fake_yt_dlp.instances[0].options


def test_entry_without_a_file_is_skipped_not_failed(fake_yt_dlp, tmp_path: Path) -> None:
    # yt-dlp writes nothing for a video already in the download archive; a real
    # failure raises instead, so this must not be reported as a failure.
    results = YouTubeDownloader(
        DownloadOptions(output_dir=tmp_path, archive_file=tmp_path / "seen.txt")
    ).download("https://youtu.be/abc123")
    assert results[0].skipped
    assert not results[0].failed
    assert not results[0].succeeded


def test_entry_with_a_missing_file_is_a_failure(fake_yt_dlp, tmp_path: Path) -> None:
    missing = tmp_path / "gone.mp4"
    fake_yt_dlp.result = VIDEO_INFO | {"requested_downloads": [{"filepath": str(missing)}]}
    results = YouTubeDownloader(DownloadOptions(output_dir=tmp_path)).download(
        "https://youtu.be/abc123"
    )
    assert results[0].failed
    assert not results[0].skipped


def test_cli_reports_skipped_and_failed_separately(
    fake_yt_dlp, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "gone.mp4"
    fake_yt_dlp.result = VIDEO_INFO | {"requested_downloads": [{"filepath": str(missing)}]}
    assert cli.main(["download", "https://youtu.be/abc", "-o", str(tmp_path), "-q"]) == 1
    out = capsys.readouterr().out
    assert "[fail]" in out
    assert "1 failed" in out
