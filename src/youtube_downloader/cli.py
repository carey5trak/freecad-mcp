"""Command line interface: ``youtube-downloader <command> URL...``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .downloader import (
    DownloadResult,
    Format,
    Progress,
    VideoInfo,
    YouTubeDownloader,
)
from .errors import YouTubeDownloaderError
from .options import DEFAULT_TEMPLATE, DownloadOptions


def _human_size(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if unit == "B":
            if value < 1024:
                return f"{int(value)}B"
        elif value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GiB"


class _ProgressPrinter:
    """Single-line progress output, with one summary line per finished file."""

    def __init__(self, stream=sys.stderr) -> None:
        self._stream = stream
        self._last_len = 0

    def __call__(self, progress: Progress) -> None:
        if progress.status == "finished":
            self._clear()
            name = Path(progress.filename).name if progress.filename else "file"
            print(f"  downloaded {name}", file=self._stream)
            return
        if progress.status != "downloading":
            return
        percent = progress.percent
        shown = f"{percent:5.1f}%" if percent is not None else "  ??.?%"
        speed = f"{_human_size(int(progress.speed))}/s" if progress.speed else "-"
        eta = f"{progress.eta}s" if progress.eta is not None else "-"
        self._write(f"  {shown}  {_human_size(progress.downloaded_bytes)}  {speed}  eta {eta}")

    def _write(self, line: str) -> None:
        padding = " " * max(0, self._last_len - len(line))
        self._stream.write(f"\r{line}{padding}")
        self._stream.flush()
        self._last_len = len(line)

    def _clear(self) -> None:
        if self._last_len:
            self._stream.write("\r" + " " * self._last_len + "\r")
            self._stream.flush()
            self._last_len = 0


def _options_from_args(args: argparse.Namespace) -> DownloadOptions:
    return DownloadOptions(
        output_dir=args.output_dir,
        filename_template=args.template,
        quality=getattr(args, "quality", "best"),
        format_spec=getattr(args, "format", None),
        container=getattr(args, "container", None),
        audio_format=getattr(args, "audio_format", "mp3"),
        audio_bitrate=getattr(args, "bitrate", "192"),
        subtitles=getattr(args, "subs", False),
        subtitle_langs=tuple(getattr(args, "sub_langs", ["en"])),
        auto_subtitles=getattr(args, "auto_subs", False),
        embed_subtitles=getattr(args, "embed_subs", False),
        embed_metadata=not args.no_metadata,
        embed_thumbnail=getattr(args, "embed_thumbnail", False),
        playlist=args.playlist,
        playlist_items=args.playlist_items,
        archive_file=args.archive,
        cookies_file=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        rate_limit=args.rate_limit,
        retries=args.retries,
        concurrent_fragments=args.concurrent_fragments,
        overwrite=args.overwrite,
    )


def _report(results: Sequence[DownloadResult]) -> int:
    downloaded = [r for r in results if r.succeeded]
    skipped = [r for r in results if r.skipped]
    failed = [r for r in results if r.failed]
    for result in results:
        if result.succeeded:
            mark, where = "ok  ", str(result.path)
        elif result.skipped:
            mark, where = "skip", "nothing to download"
        else:
            mark, where = "fail", f"{result.path} is missing"
        print(f"[{mark}] {result.title or result.video_id}: {where}")
    summary = f"{len(downloaded)}/{len(results)} downloaded"
    if skipped:
        summary += f", {len(skipped)} skipped"
    if failed:
        summary += f", {len(failed)} failed"
    print(summary)
    return 1 if failed else 0


def _print_info(info: VideoInfo) -> None:
    print(info.title)
    print(f"  id        {info.video_id}")
    print(f"  uploader  {info.uploader or '-'}")
    print(f"  duration  {info.duration_str}")
    print(f"  uploaded  {info.upload_date or '-'}")
    print(f"  views     {info.view_count if info.view_count is not None else '-'}")
    print(f"  url       {info.url}")


def _kind_of(fmt: Format) -> str:
    if fmt.note:
        return fmt.note
    if not fmt.has_audio:
        return "video only"
    if not fmt.has_video:
        return "audio only"
    return ""


def _print_formats(formats: Sequence[Format]) -> None:
    header = f"{'id':<10} {'ext':<5} {'resolution':<12} {'fps':>5} {'size':>10}  note"
    print(header)
    print("-" * len(header))
    for fmt in formats:
        print(
            f"{fmt.format_id:<10} {fmt.ext:<5} {(fmt.resolution or '-'):<12} "
            f"{(fmt.fps or 0):>5.0f} {_human_size(fmt.filesize):>10}  {_kind_of(fmt)}"
        )


def _info_as_dict(info: VideoInfo) -> dict:
    return {
        "id": info.video_id,
        "title": info.title,
        "url": info.url,
        "uploader": info.uploader,
        "duration": info.duration,
        "upload_date": info.upload_date,
        "view_count": info.view_count,
        "thumbnail": info.thumbnail,
        "description": info.description,
    }


def _add_urls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("urls", nargs="+", metavar="URL", help="YouTube video or playlist URL")
    parser.add_argument(
        "--any-site", action="store_true", help="allow non-YouTube URLs that yt-dlp supports"
    )


def _add_download_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=Path("downloads"),
        help="directory to write files into (default: ./downloads)",
    )
    parser.add_argument(
        "-t", "--template", default=DEFAULT_TEMPLATE,
        # argparse runs help through %-formatting, so the template's own
        # %(title)s placeholders have to be escaped.
        help=f"yt-dlp output template (default: {DEFAULT_TEMPLATE!r})".replace("%", "%%"),
    )
    parser.add_argument("--playlist", action="store_true", help="download every entry of a playlist URL")
    parser.add_argument("--playlist-items", help="playlist selection, e.g. '1-5,8'")
    parser.add_argument("--archive", type=Path, help="archive file recording what has been downloaded")
    parser.add_argument("--cookies", type=Path, help="cookies.txt file for restricted videos")
    parser.add_argument("--cookies-from-browser", help="load cookies from a browser, e.g. firefox")
    parser.add_argument("--rate-limit", help="cap download speed, e.g. 500K or 2M")
    parser.add_argument("--retries", type=int, default=3, help="retries per download (default: 3)")
    parser.add_argument(
        "--concurrent-fragments", type=int, default=1,
        help="fragments to fetch in parallel per file (default: 1)",
    )
    parser.add_argument("--overwrite", action="store_true", help="re-download and replace existing files")
    parser.add_argument("--no-metadata", action="store_true", help="do not write metadata into the file")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-downloader",
        description="Download YouTube videos, audio and metadata via yt-dlp.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="download video files")
    _add_urls(download)
    _add_download_common(download)
    download.add_argument(
        "--quality", default="best",
        help="'best', 'worst', or a maximum height such as 1080p (default: best)",
    )
    download.add_argument("--format", help="raw yt-dlp format selector, overrides --quality")
    download.add_argument("--container", help="merge into this container, e.g. mp4 or mkv")
    download.add_argument("--subs", action="store_true", help="download subtitles")
    download.add_argument("--auto-subs", action="store_true", help="include auto-generated subtitles")
    download.add_argument("--sub-langs", nargs="+", default=["en"], help="subtitle languages (default: en)")
    download.add_argument("--embed-subs", action="store_true", help="embed subtitles into the video file")
    download.add_argument("--embed-thumbnail", action="store_true", help="embed the thumbnail as cover art")

    audio = sub.add_parser("audio", help="download audio only")
    _add_urls(audio)
    _add_download_common(audio)
    audio.add_argument("--audio-format", default="mp3", help="mp3, m4a, opus, flac, wav (default: mp3)")
    audio.add_argument("--bitrate", default="192", help="target bitrate in kbps (default: 192)")
    audio.add_argument("--embed-thumbnail", action="store_true", help="embed the thumbnail as cover art")

    info = sub.add_parser("info", help="print metadata without downloading")
    _add_urls(info)
    info.add_argument("--json", action="store_true", help="emit JSON instead of a summary")

    formats = sub.add_parser("formats", help="list the streams available for a URL")
    _add_urls(formats)

    serve = sub.add_parser("serve", help="open the browser UI, backed by a local helper")
    serve.add_argument(
        "-o", "--output-dir", type=Path, default=Path("downloads"),
        help="directory to write files into (default: ./downloads)",
    )
    serve.add_argument(
        "-t", "--template", default=DEFAULT_TEMPLATE,
        help=f"yt-dlp output template (default: {DEFAULT_TEMPLATE!r})".replace("%", "%%"),
    )
    serve.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    serve.add_argument(
        "--host", default="127.0.0.1",
        help="address to bind (default: 127.0.0.1, reachable only from this machine)",
    )
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    serve.add_argument(
        "--any-site", action="store_true", help="allow non-YouTube URLs that yt-dlp supports"
    )
    serve.add_argument("--cookies", type=Path, help="cookies.txt file for restricted videos")
    serve.add_argument("--cookies-from-browser", help="load cookies from a browser, e.g. firefox")

    return parser


def _run_metadata_command(args: argparse.Namespace) -> int:
    downloader = YouTubeDownloader(allow_other_sites=args.any_site)
    payload: list[dict] = []
    for url in args.urls:
        info = downloader.probe(url)
        if args.command == "formats":
            _print_formats(info.formats)
        elif args.json:
            payload.append(_info_as_dict(info))
        else:
            _print_info(info)
    if payload:
        print(json.dumps(payload if len(payload) > 1 else payload[0], indent=2))
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    import webbrowser

    from .webapp import LOOPBACK, create_server, serve_urls

    options = DownloadOptions(
        output_dir=args.output_dir,
        filename_template=args.template,
        cookies_file=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
    )
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    server, token = create_server(
        options, host=args.host, port=args.port, allow_other_sites=args.any_site
    )
    urls = serve_urls(args.host, server.server_address[1], token)
    width = max(len(label) for label, _ in urls)

    print("YouTube downloader UI")
    for label, url in urls:
        print(f"  {label:<{width}}  {url}")
    print(f"Saving into: {Path(args.output_dir).resolve()}")
    if args.host not in LOOPBACK:
        print(
            "warning: this helper downloads whatever it is asked to. Binding it to a "
            "non-loopback address exposes it to your network.",
            file=sys.stderr,
        )
    print("Access token:", token)
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        webbrowser.open(urls[0][1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _run_serve(args)
        if args.command in {"info", "formats"}:
            return _run_metadata_command(args)

        options = _options_from_args(args)
        if args.command == "audio":
            options = replace(options, audio_only=True)
        downloader = YouTubeDownloader(options, allow_other_sites=args.any_site)
        progress = None if args.quiet else _ProgressPrinter()
        return _report(downloader.download(args.urls, progress=progress))
    except YouTubeDownloaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
