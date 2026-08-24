"""Tests for the local web helper. No network access required."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from youtube_downloader.options import DownloadOptions
from youtube_downloader.webapp import (
    DownloadService,
    JobRegistry,
    _options_from_request,
    create_server,
)

from _fakes import VIDEO_INFO

# The sandbox exports HTTPS_PROXY; talking to our own loopback server must not
# go through it.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

TOKEN = "test-token"


def request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str | None = TOKEN,
) -> tuple[int, Any, dict[str, str]]:
    """Make a request, returning (status, parsed body, headers)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token is not None:
        req.add_header("X-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=10) as response:
            raw, status, headers = response.read(), response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw, status, headers = exc.read(), exc.code, dict(exc.headers)
    content_type = headers.get("Content-Type", "")
    parsed = json.loads(raw) if raw and "json" in content_type else raw
    return status, parsed, headers


@pytest.fixture
def server(fake_yt_dlp, tmp_path: Path):
    """A running helper bound to an ephemeral loopback port."""
    options = DownloadOptions(output_dir=tmp_path / "out")
    (tmp_path / "out").mkdir()
    httpd, token = create_server(options, host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{httpd.server_address[1]}",
            token=token,
            output_dir=tmp_path / "out",
            tmp_path=tmp_path,
            fake=fake_yt_dlp,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def wait_for_job(base: str, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """Poll a job until it leaves the running state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, job, _ = request(base, f"/api/jobs/{job_id}")
        assert status == 200
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# -- request mapping ---------------------------------------------------


def test_options_from_request_video() -> None:
    options = _options_from_request(
        DownloadOptions(), {"mode": "video", "quality": "720p", "container": "mkv", "subtitles": True}
    )
    assert options.quality == "720p"
    assert options.container == "mkv"
    assert options.subtitles and options.embed_subtitles
    assert not options.audio_only


def test_options_from_request_audio_ignores_video_settings() -> None:
    options = _options_from_request(
        DownloadOptions(),
        {"mode": "audio", "quality": "720p", "container": "mkv", "audioFormat": "flac",
         "bitrate": "320", "subtitles": True},
    )
    assert options.audio_only
    assert options.audio_format == "flac"
    assert options.audio_bitrate == "320"
    assert options.container is None
    assert options.quality == "best"
    # Subtitles cannot be embedded into an audio-only file.
    assert not options.embed_subtitles


def test_options_from_request_keeps_server_defaults(tmp_path: Path) -> None:
    base = DownloadOptions(output_dir=tmp_path, cookies_from_browser="firefox")
    options = _options_from_request(base, {"mode": "video"})
    assert options.output_dir == tmp_path
    assert options.cookies_from_browser == "firefox"


# -- job registry ------------------------------------------------------


def test_job_registry_round_trip() -> None:
    registry = JobRegistry()
    job = registry.create("https://youtu.be/a", "video")
    assert registry.get(job.id) is job
    assert [j.id for j in registry.all()] == [job.id]
    registry.update(job, status="done")
    assert registry.get(job.id).status == "done"
    assert registry.remove(job.id)
    assert not registry.remove(job.id)
    assert registry.all() == []


# -- serving the page --------------------------------------------------


def test_index_is_served_without_a_token(server) -> None:
    status, body, headers = request(server.base, "/", token=None)
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"<title>YouTube downloader</title>" in body


def test_health_needs_no_token(server) -> None:
    status, body, _ = request(server.base, "/api/health", token=None)
    assert status == 200
    assert body == {"status": "ok"}


def test_preflight_is_answered(server) -> None:
    status, _, headers = request(server.base, "/api/jobs", method="OPTIONS", token=None)
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "*"


# -- authentication ----------------------------------------------------


def test_api_rejects_a_missing_token(server) -> None:
    status, body, _ = request(server.base, "/api/jobs", token=None)
    assert status == 401
    assert "token" in body["error"]


def test_api_rejects_a_wrong_token(server) -> None:
    status, _, _ = request(server.base, "/api/jobs", token="nope")
    assert status == 401


def test_download_rejects_a_missing_token(server) -> None:
    status, _, _ = request(
        server.base, "/api/download", method="POST", body={"url": "https://youtu.be/a"}, token=None
    )
    assert status == 401


def test_token_may_be_passed_as_a_query_parameter(server) -> None:
    status, _, _ = request(server.base, f"/api/jobs?t={server.token}", token=None)
    assert status == 200


# -- metadata ----------------------------------------------------------


def test_info_returns_metadata(server) -> None:
    status, body, _ = request(
        server.base, "/api/info", method="POST", body={"url": "https://youtu.be/abc123"}
    )
    assert status == 200
    assert body["title"] == "Example Video"
    assert body["durationText"] == "1:02:05"
    assert len(body["formats"]) == 3
    assert body["formats"][1]["hasVideo"] and not body["formats"][1]["hasAudio"]


def test_info_rejects_a_non_youtube_url(server) -> None:
    status, body, _ = request(
        server.base, "/api/info", method="POST", body={"url": "https://vimeo.com/1"}
    )
    assert status == 400
    assert "YouTube host" in body["error"]


def test_info_allows_other_sites_when_asked(server) -> None:
    status, _, _ = request(
        server.base, "/api/info", method="POST",
        body={"url": "https://vimeo.com/1", "anySite": True},
    )
    assert status == 200


def test_a_url_is_required(server) -> None:
    status, body, _ = request(server.base, "/api/info", method="POST", body={"url": "  "})
    assert status == 400
    assert "url is required" in body["error"]


# -- downloading -------------------------------------------------------


def test_download_runs_and_exposes_the_file(server) -> None:
    target = server.output_dir / "Example Video [abc123].mp4"
    target.write_bytes(b"video payload")
    server.fake.result = VIDEO_INFO | {"requested_downloads": [{"filepath": str(target)}]}
    server.fake.progress_events = [
        {"status": "downloading", "filename": str(target), "downloaded_bytes": 100,
         "total_bytes": 200, "speed": 1000.0, "eta": 1},
        {"status": "finished", "filename": str(target)},
    ]

    status, job, _ = request(
        server.base, "/api/download", method="POST", body={"url": "https://youtu.be/abc123"}
    )
    assert status == 202

    finished = wait_for_job(server.base, job["id"])
    assert finished["status"] == "done"
    assert finished["title"] == "Example Video"
    assert finished["percent"] == 100.0
    assert finished["totalBytes"] == 200
    assert [f["name"] for f in finished["files"]] == ["Example Video [abc123].mp4"]

    status, content, headers = request(
        server.base, f"/api/files/{job['id']}/0?t={server.token}", token=None
    )
    assert status == 200
    assert content == b"video payload"
    assert "attachment" in headers["Content-Disposition"]


def test_download_reports_a_failure(server) -> None:
    from _fakes import FakeDownloadError

    server.fake.error = FakeDownloadError("video unavailable")
    _, job, _ = request(
        server.base, "/api/download", method="POST", body={"url": "https://youtu.be/abc123"}
    )
    finished = wait_for_job(server.base, job["id"])
    assert finished["status"] == "error"
    assert "video unavailable" in finished["error"]


def test_download_reports_a_skip(server) -> None:
    # The fake writes no file, which is what an already-archived video looks like.
    _, job, _ = request(
        server.base, "/api/download", method="POST", body={"url": "https://youtu.be/abc123"}
    )
    finished = wait_for_job(server.base, job["id"])
    assert finished["status"] == "skipped"
    assert finished["files"] == []


def test_jobs_are_listed_and_removable(server) -> None:
    _, job, _ = request(
        server.base, "/api/download", method="POST", body={"url": "https://youtu.be/abc123"}
    )
    wait_for_job(server.base, job["id"])

    status, body, _ = request(server.base, "/api/jobs")
    assert [j["id"] for j in body["jobs"]] == [job["id"]]

    status, body, _ = request(server.base, f"/api/jobs/{job['id']}", method="DELETE")
    assert status == 200 and body["removed"] is True

    status, body, _ = request(server.base, "/api/jobs")
    assert body["jobs"] == []


def test_removing_an_unknown_job_is_a_404(server) -> None:
    status, _, _ = request(server.base, "/api/jobs/nope", method="DELETE")
    assert status == 404


# -- file serving is confined to the output directory ------------------


def test_files_outside_the_output_directory_are_refused(server) -> None:
    outside = server.tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"should not be served")
    server.fake.result = VIDEO_INFO | {"requested_downloads": [{"filepath": str(outside)}]}

    _, job, _ = request(
        server.base, "/api/download", method="POST", body={"url": "https://youtu.be/abc123"}
    )
    finished = wait_for_job(server.base, job["id"])
    assert finished["status"] == "done"  # the file exists, so the download worked

    status, body, _ = request(
        server.base, f"/api/files/{job['id']}/0?t={server.token}", token=None
    )
    assert status == 404
    assert "no longer available" in body["error"]


def test_unknown_file_index_is_a_404(server) -> None:
    _, job, _ = request(
        server.base, "/api/download", method="POST", body={"url": "https://youtu.be/abc123"}
    )
    wait_for_job(server.base, job["id"])
    status, _, _ = request(server.base, f"/api/files/{job['id']}/7?t={server.token}", token=None)
    assert status == 404


def test_unknown_endpoints_are_404(server) -> None:
    assert request(server.base, "/api/nope")[0] == 404
    assert request(server.base, "/api/nope", method="POST", body={"url": "x"})[0] == 404


# -- the URLs printed at startup ---------------------------------------


def test_serve_urls_on_loopback_offers_only_the_local_address() -> None:
    from youtube_downloader.webapp import serve_urls

    urls = serve_urls("127.0.0.1", 8765, "tok")
    assert urls == [("On this machine", "http://127.0.0.1:8765/?t=tok")]


def test_serve_urls_on_a_wildcard_bind_offers_reachable_addresses() -> None:
    # Binding to 0.0.0.0 means the point is to reach it from another device,
    # so a loopback URL alone would be useless.
    from youtube_downloader.webapp import serve_urls

    urls = serve_urls("0.0.0.0", 8765, "tok", addresses=["192.168.1.5", "10.0.0.9"])
    assert urls == [
        ("On this machine", "http://127.0.0.1:8765/?t=tok"),
        ("On another device", "http://192.168.1.5:8765/?t=tok"),
        ("On another device", "http://10.0.0.9:8765/?t=tok"),
    ]


def test_serve_urls_trusts_an_explicit_bind_address() -> None:
    from youtube_downloader.webapp import serve_urls

    urls = serve_urls("192.168.1.5", 8765, "tok", addresses=["10.0.0.9"])
    assert urls[-1] == ("On another device", "http://192.168.1.5:8765/?t=tok")
    assert all("10.0.0.9" not in url for _, url in urls)


def test_serve_urls_brackets_ipv6() -> None:
    from youtube_downloader.webapp import serve_urls

    urls = serve_urls("::", 8765, "tok", addresses=["fe80::1"])
    assert urls[-1] == ("On another device", "http://[fe80::1]:8765/?t=tok")


def test_serve_urls_survives_having_no_reachable_address() -> None:
    from youtube_downloader.webapp import serve_urls

    assert serve_urls("0.0.0.0", 8765, "tok", addresses=[]) == [
        ("On this machine", "http://127.0.0.1:8765/?t=tok")
    ]


def test_lan_addresses_never_returns_loopback() -> None:
    from youtube_downloader.webapp import lan_addresses

    addresses = lan_addresses()
    assert isinstance(addresses, list)
    assert all(not a.startswith("127.") for a in addresses)
    assert len(set(addresses)) == len(addresses)
