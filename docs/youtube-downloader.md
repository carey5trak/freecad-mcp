# YouTube downloader

A standalone command line tool and Python API for downloading YouTube videos,
extracting audio, and reading video metadata. It wraps
[yt-dlp](https://github.com/yt-dlp/yt-dlp) behind a small typed interface.

It is independent of the FreeCAD MCP server that otherwise fills this
repository — nothing here talks to FreeCAD, and the MCP server does not import
it.

## Install

The tool ships with this project, so a normal sync picks it up:

```bash
uv sync
uv run youtube-downloader --help
```

[ffmpeg](https://ffmpeg.org/) must be on `PATH` for audio extraction, container
conversion, and embedding subtitles or thumbnails. Without it, plain video
downloads still work: the tool falls back to the best single stream that already
carries both video and audio, and says so.

## Command line

```bash
# Best available quality into ./downloads
uv run youtube-downloader download "https://youtu.be/VIDEO_ID"

# Cap the resolution and pick a container
uv run youtube-downloader download "https://youtu.be/VIDEO_ID" --quality 1080p --container mp4

# Audio only, as a 320kbps mp3, into a chosen directory
uv run youtube-downloader audio "https://youtu.be/VIDEO_ID" --audio-format mp3 --bitrate 320 -o ~/Music

# A whole playlist, skipping anything already recorded in the archive file
uv run youtube-downloader download "https://youtube.com/playlist?list=LIST_ID" \
    --playlist --archive ~/.yt-archive.txt

# English subtitles burned into the container
uv run youtube-downloader download "https://youtu.be/VIDEO_ID" --subs --embed-subs

# Metadata and available streams, without downloading
uv run youtube-downloader info "https://youtu.be/VIDEO_ID"
uv run youtube-downloader info "https://youtu.be/VIDEO_ID" --json
uv run youtube-downloader formats "https://youtu.be/VIDEO_ID"
```

### Commands

| Command | Purpose |
| --- | --- |
| `download` | Download video files. |
| `audio` | Download and keep only the audio track. |
| `info` | Print metadata; `--json` for machine-readable output. |
| `formats` | List the streams available for a URL. |

### Useful flags

| Flag | Effect |
| --- | --- |
| `-o, --output-dir` | Where files are written (default `./downloads`). |
| `-t, --template` | yt-dlp output template (default `%(title)s [%(id)s].%(ext)s`). |
| `--quality` | `best`, `worst`, or a maximum height such as `720p`. |
| `--format` | A raw yt-dlp format selector, overriding `--quality`. |
| `--playlist`, `--playlist-items` | Expand playlist URLs, optionally selecting entries like `1-5,8`. |
| `--archive` | Record downloads so repeat runs skip them. |
| `--cookies`, `--cookies-from-browser` | Authenticate for age-restricted, private, or member-only videos. |
| `--rate-limit` | Cap the download speed, e.g. `500K` or `2M`. |
| `--concurrent-fragments` | Fetch several fragments per file in parallel. |
| `--any-site` | Accept non-YouTube URLs that yt-dlp supports. |
| `-q, --quiet` | Suppress the progress display. |

By default only YouTube hosts are accepted, so a mistyped URL fails immediately
rather than being handed to yt-dlp's generic extractor. `--any-site` lifts that.

### Sign-in and bot checks

YouTube may answer with "Sign in to confirm you're not a bot", especially from
datacenter IPs. Pass browser cookies to get past it:

```bash
uv run youtube-downloader download "https://youtu.be/VIDEO_ID" --cookies-from-browser firefox
```

## Python API

```python
from youtube_downloader import DownloadOptions, YouTubeDownloader

options = DownloadOptions(
    output_dir="clips",
    quality="1080p",
    container="mp4",
    subtitles=True,
    subtitle_langs=("en", "ja"),
)
downloader = YouTubeDownloader(options)

info = downloader.probe("https://youtu.be/VIDEO_ID")
print(info.title, info.duration_str, info.uploader)

for result in downloader.download("https://youtu.be/VIDEO_ID"):
    print(result.path, result.filesize)

for result in downloader.download_audio("https://youtu.be/VIDEO_ID", audio_format="flac"):
    print(result.path)
```

Progress is reported through a callback:

```python
def on_progress(progress):
    if progress.percent is not None:
        print(f"{progress.percent:.1f}%")

downloader.download("https://youtu.be/VIDEO_ID", progress=on_progress)
```

Anything yt-dlp supports but this API does not expose can be passed straight
through, and wins over the generated settings:

```python
DownloadOptions(extra_ydl_options={"geo_bypass": True, "source_address": "0.0.0.0"})
```

### Errors

All failures derive from `YouTubeDownloaderError`:

| Exception | Raised when |
| --- | --- |
| `InvalidURLError` | The URL is not http(s), or not a YouTube host without `allow_other_sites`. |
| `MetadataError` | Metadata could not be read (private, removed, or blocked video). |
| `DownloadFailedError` | yt-dlp failed during a download. |
| `FFmpegMissingError` | The options need ffmpeg and no binary was found. |

## Tests

```bash
uv run pytest
```

The suite stubs yt-dlp out, so it needs no network access and downloads nothing.

## Terms of use

Downloading is subject to YouTube's Terms of Service and to the copyright in the
material. Use it for content you own, content licensed for reuse, or where the
copyright holder permits it.
