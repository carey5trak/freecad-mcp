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

## Browser UI

A browser cannot fetch YouTube's streams by itself — YouTube sends no
cross-origin headers, so page JavaScript is not permitted to read them. The UI
therefore comes with a small helper that runs on your own machine and does the
downloading; the page just drives it.

```bash
uv run youtube-downloader serve
```

That starts the helper, prints an address, and opens your browser:

```
YouTube downloader UI: http://127.0.0.1:8765/?t=Lmicv0EJePNHqCWUHYWm7q-W
Saving into: /home/you/downloads
Access token: Lmicv0EJePNHqCWUHYWm7q-W
Press Ctrl+C to stop.
```

The page offers the same choices as the CLI — video or audio, quality,
container, subtitles, whole playlists — shows live progress, and gives a save
link for each finished file. Files are written to the helper's output directory
regardless; the link is there for when your browser's download folder is more
convenient.

| Flag | Effect |
| --- | --- |
| `-o, --output-dir` | Where files are written (default `./downloads`). |
| `--port` | Port to listen on (default `8765`). |
| `--host` | Address to bind (default `127.0.0.1`). |
| `--no-browser` | Do not open a browser window. |
| `--any-site` | Accept non-YouTube URLs by default. |
| `--cookies`, `--cookies-from-browser` | Authenticate for restricted videos. |

### Opening the HTML file directly

`src/youtube_downloader/static/index.html` is a self-contained page — no build
step, no external requests. Opening it straight from disk works too: it will
say it cannot find the helper and ask for the address and access token that
`serve` prints. Letting `serve` host the page is simpler, since it fills both
in for you.

### Security

The helper downloads whatever it is asked to, so it is built to be reachable
only by you:

- It binds to `127.0.0.1`, so nothing outside your machine can see it.
- Every API call must carry the access token minted at startup. A random web
  page you happen to have open cannot read that token, so it cannot drive the
  helper.
- Saved files are served only from the configured output directory, and only
  ones the helper itself recorded.

Binding it to a non-loopback address with `--host` exposes it to your network,
and it prints a warning when you do.

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
