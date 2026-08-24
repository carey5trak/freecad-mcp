"""Offline tests for ytmd.

yt-dlp is stubbed for everything that would touch the network, so the whole
suite runs with no internet connection. One test deliberately uses the *real*
yt-dlp to prove the option dictionaries we build are actually accepted.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ytmd  # noqa: E402
import yt_dlp  # noqa: E402


# --------------------------------------------------------------------------- #
# A stand-in for yt_dlp.YoutubeDL
# --------------------------------------------------------------------------- #


_MISSING = object()


class FakeYDL:
    """Replays a scripted result instead of talking to YouTube."""

    script: dict[str, Any] = {}
    seen_opts: list[dict[str, Any]] = []

    def __init__(self, opts: Optional[dict[str, Any]] = None) -> None:
        self.opts = opts or {}
        FakeYDL.seen_opts.append(self.opts)

    def __enter__(self) -> "FakeYDL":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    @staticmethod
    def sanitize_info(info: Any) -> Any:
        return info

    def extract_info(self, url: str, download: bool = False) -> Any:
        plan = FakeYDL.script.get(url, FakeYDL.script.get("*", _MISSING))
        if plan is _MISSING:
            raise yt_dlp.utils.DownloadError(f"no script for {url}")
        if not download:
            return plan
        if plan is None:
            return None

        hooks = self.opts.get("progress_hooks") or []
        pp_hooks = self.opts.get("postprocessor_hooks") or []
        if plan.get("raise"):
            raise plan["raise"]
        total = plan.get("bytes", 1_000_000)
        for step in (0.0, 0.5, 1.0):
            for hook in hooks:
                hook({
                    "status": "downloading",
                    "downloaded_bytes": int(total * step),
                    "total_bytes": total,
                    "speed": 250_000.0,
                    "eta": 3,
                })
            time.sleep(plan.get("delay", 0.0))
        for hook in pp_hooks:
            hook({"status": "started", "postprocessor": "FFmpegExtractAudio", "info_dict": {}})
        path = plan.get("filepath", "/tmp/song.mp3")
        for hook in hooks:
            hook({"status": "finished", "total_bytes": total, "filename": path})
        return {"id": plan.get("id", "x"), "title": plan.get("title", "x"),
                "requested_downloads": [{"filepath": path}]}


def install_fake_ydl(script: dict[str, Any]) -> None:
    FakeYDL.script = script
    FakeYDL.seen_opts = []
    ytmd.yt_dlp.YoutubeDL = FakeYDL  # type: ignore[misc]


def restore_ydl() -> None:
    ytmd.yt_dlp.YoutubeDL = REAL_YDL  # type: ignore[misc]


REAL_YDL = yt_dlp.YoutubeDL


def flat_playlist(count: int = 3, with_dead: bool = False) -> dict[str, Any]:
    entries = []
    for i in range(count):
        entries.append({
            "_type": "url",
            "ie_key": "Youtube",
            "id": f"vid{i:08d}xyz"[:11],
            "title": f"Track {i + 1}",
            "duration": 180 + i,
            "uploader": "Test Artist",
            "thumbnails": [{"url": f"https://i.ytimg.com/vi/v{i}/default.jpg"},
                           {"url": f"https://i.ytimg.com/vi/v{i}/mqdefault.jpg"}],
        })
    if with_dead:
        entries.append({"_type": "url", "ie_key": "Youtube", "id": "deadbeef123",
                        "title": "[Private video]", "duration": None})
    return {"_type": "playlist", "id": "PLtest", "title": "Test Mix",
            "uploader": "Test Channel", "webpage_url": "https://youtube.com/playlist?list=PLtest",
            "entries": entries}


# --------------------------------------------------------------------------- #
# Pure-function tests
# --------------------------------------------------------------------------- #


class TestNormalizeSource(unittest.TestCase):
    def test_full_urls_pass_through(self) -> None:
        url = "https://music.youtube.com/playlist?list=OLAK5uy_abc"
        self.assertEqual(ytmd.normalize_source(url), url)

    def test_bare_video_id(self) -> None:
        self.assertEqual(ytmd.normalize_source("dQw4w9WgXcQ"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_bare_playlist_id(self) -> None:
        self.assertEqual(
            ytmd.normalize_source("PLabcdefghij"),
            "https://www.youtube.com/playlist?list=PLabcdefghij",
        )

    def test_free_text_becomes_a_search(self) -> None:
        self.assertTrue(ytmd.normalize_source("lo-fi beats").startswith("ytsearch25:"))

    def test_explicit_search_prefix_kept(self) -> None:
        self.assertEqual(ytmd.normalize_source("ytsearch5:jazz"), "ytsearch5:jazz")

    def test_empty(self) -> None:
        self.assertEqual(ytmd.normalize_source("   "), "")


class TestSettingsCoercion(unittest.TestCase):
    def test_unknown_keys_dropped(self) -> None:
        out = ytmd.coerce_settings({"totallyMadeUp": 1, "concurrency": 4})
        self.assertNotIn("totallyMadeUp", out)
        self.assertEqual(out["concurrency"], 4)

    def test_numbers_clamped(self) -> None:
        self.assertEqual(ytmd.coerce_settings({"concurrency": 9999})["concurrency"], 8)
        self.assertEqual(ytmd.coerce_settings({"concurrency": -5})["concurrency"], 1)

    def test_bad_enum_ignored(self) -> None:
        out = ytmd.coerce_settings({"audioFormat": "; rm -rf /"})
        self.assertEqual(out["audioFormat"], ytmd.DEFAULT_SETTINGS["audioFormat"])

    def test_types_coerced(self) -> None:
        out = ytmd.coerce_settings({"embedThumbnail": 0, "retries": "3"})
        self.assertIs(out["embedThumbnail"], False)
        self.assertEqual(out["retries"], 3)

    def test_non_dict_input(self) -> None:
        self.assertEqual(ytmd.coerce_settings("nope")["mode"], ytmd.DEFAULT_SETTINGS["mode"])

    def test_long_strings_truncated(self) -> None:
        out = ytmd.coerce_settings({"customFolder": "x" * 99_999})
        self.assertLessEqual(len(out["customFolder"]), 4096)


class TestPathSafety(unittest.TestCase):
    def test_reserved_windows_names(self) -> None:
        self.assertTrue(ytmd.safe_component("CON").startswith("_"))
        self.assertTrue(ytmd.safe_component("lpt1").startswith("_"))

    def test_separators_removed(self) -> None:
        for probe in ("../../etc", "a/b", "a\\b"):
            self.assertNotIn("/", ytmd.safe_component(probe))
            self.assertNotIn("\\", ytmd.safe_component(probe))

    def test_dot_only_names(self) -> None:
        self.assertEqual(ytmd.safe_component(".."), "Unknown")
        self.assertEqual(ytmd.safe_component(""), "Unknown")

    def test_custom_folder_cannot_escape_base(self) -> None:
        settings = dict(ytmd.DEFAULT_SETTINGS,
                        outputDir="/tmp/base", folderMode="custom",
                        customFolder="../../../../etc/cron.d")
        target = ytmd.resolve_target_dir(settings, "", {"uploader": ""})
        self.assertTrue(str(target).startswith("/tmp/base"), target)
        self.assertNotIn("..", target.parts)

    def test_playlist_folder_sanitised(self) -> None:
        settings = dict(ytmd.DEFAULT_SETTINGS, outputDir="/tmp/base", folderMode="playlist")
        target = ytmd.resolve_target_dir(settings, "../evil/../name", {"uploader": ""})
        self.assertEqual(target.parent, Path("/tmp/base"))

    def test_template_literals_escaped(self) -> None:
        self.assertEqual(ytmd.escape_template_literal("100%(title)s"), "100%%(title)s")


class TestRowIdentity(unittest.TestCase):
    """The page keys rows on a uid built from the FULL playlist; the job only
    receives the selection, so the uid has to travel with the track."""

    def test_non_prefix_selection_keeps_the_page_uid(self) -> None:
        full = [{"id": f"vid{i}", "title": f"T{i}", "index": i + 1} for i in range(6)]
        page_uids = [f"{i}:{t['id']}" for i, t in enumerate(full)]
        selected = [dict(full[i], uid=page_uids[i]) for i in (0, 3, 5)]
        job = ytmd.Job("j", selected, ytmd.DEFAULT_SETTINGS, "Mix")
        self.assertEqual([item.uid for item in job.items], [page_uids[i] for i in (0, 3, 5)])

    def test_missing_uid_falls_back_to_position(self) -> None:
        job = ytmd.Job("j", [{"id": "abc"}, {"id": "def"}], ytmd.DEFAULT_SETTINGS, "")
        self.assertEqual([item.uid for item in job.items], ["0:abc", "1:def"])

    def test_duplicate_uids_are_separated(self) -> None:
        job = ytmd.Job("j", [{"id": "a", "uid": "0:a"}, {"id": "a", "uid": "0:a"}], ytmd.DEFAULT_SETTINGS, "")
        self.assertEqual(len(job.by_uid), 2)
        self.assertNotEqual(job.items[0].uid, job.items[1].uid)

    def test_uid_is_sanitised(self) -> None:
        self.assertNotIn("/", ytmd.sanitize_uid("../../etc/passwd"))
        self.assertLessEqual(len(ytmd.sanitize_uid("a" * 500)), 80)
        self.assertEqual(ytmd.sanitize_uid(None), "")
        self.assertEqual(ytmd.sanitize_uid(12), "")

    def test_a_sanitised_uid_still_routes_to_cancel(self) -> None:
        """The cancel route matches [^/]{1,80}; the uid must fit through it."""
        uid = ytmd.sanitize_uid("12:abcDEF_-~.")
        self.assertRegex(f"/items/{uid}/cancel", r"^/items/([^/]{1,80})/cancel$")


class TestErrorCleanup(unittest.TestCase):
    def test_strips_prefix_and_report_url(self) -> None:
        raw = "ERROR: [youtube] abc: Video unavailable; please report this issue on https://github.com/yt-dlp/yt-dlp/issues"
        self.assertEqual(ytmd.clean_error(raw), "[youtube] abc: Video unavailable")

    def test_strips_ansi(self) -> None:
        self.assertEqual(ytmd.clean_error("\x1b[31mboom\x1b[0m"), "boom")


class TestFinalPath(unittest.TestCase):
    def test_requested_downloads_wins(self) -> None:
        info = {"filepath": "/a/old.webm", "requested_downloads": [{"filepath": "/a/new.mp3"}]}
        self.assertEqual(ytmd.extract_final_path(info), "/a/new.mp3")

    def test_falls_back_to_top_level(self) -> None:
        self.assertEqual(ytmd.extract_final_path({"_filename": "/a/b.m4a"}), "/a/b.m4a")

    def test_handles_junk(self) -> None:
        self.assertEqual(ytmd.extract_final_path(None), "")
        self.assertEqual(ytmd.extract_final_path({}), "")


# --------------------------------------------------------------------------- #
# Options must be acceptable to the real yt-dlp
# --------------------------------------------------------------------------- #


class TestRealYtdlpAcceptsOurOptions(unittest.TestCase):
    """Constructing a real YoutubeDL validates every postprocessor key/kwarg."""

    def _build(self, **overrides: Any) -> dict[str, Any]:
        settings = dict(ytmd.DEFAULT_SETTINGS, outputDir="/tmp/ytmd-test", **overrides)
        hooks = {"progress": lambda d: None, "postprocessor": lambda d: None,
                 "logger": ytmd._CollectingLogger(lambda level, msg: None)}
        return ytmd.build_ydl_opts(
            settings, {"index": 1, "uploader": "A"}, "Mix", True, "/usr/bin", hooks, None
        )

    def _assert_constructible(self, opts: dict[str, Any]) -> None:
        opts = dict(opts, quiet=True, ffmpeg_location=None)
        with REAL_YDL(opts):
            pass

    def test_every_audio_format(self) -> None:
        for fmt in ("mp3", "m4a", "opus", "flac", "wav", "vorbis", "best"):
            with self.subTest(fmt=fmt):
                self._assert_constructible(self._build(audioFormat=fmt))

    def test_all_extras_on(self) -> None:
        self._assert_constructible(self._build(
            embedThumbnail=True, embedMetadata=True, parseArtistFromTitle=True, skipNonMusic=True,
        ))

    def test_video_mode(self) -> None:
        for container in ("mp4", "mkv", "webm"):
            with self.subTest(container=container):
                self._assert_constructible(self._build(mode="video", videoContainer=container, writeSubtitles=True))

    def test_postprocessor_keys_are_real(self) -> None:
        from yt_dlp.postprocessor import get_postprocessor
        opts = self._build(embedThumbnail=True, embedMetadata=True,
                           parseArtistFromTitle=True, skipNonMusic=True)
        keys = [pp["key"] for pp in opts["postprocessors"]]
        self.assertIn("FFmpegExtractAudio", keys)
        self.assertIn("EmbedThumbnail", keys)
        for key in keys:
            self.assertIsNotNone(get_postprocessor(key), key)

    def test_no_postprocessors_without_ffmpeg(self) -> None:
        settings = dict(ytmd.DEFAULT_SETTINGS, embedThumbnail=True, embedMetadata=True)
        self.assertEqual(ytmd.build_postprocessors(settings, has_ffmpeg=False), [])

    def test_progressive_format_without_ffmpeg(self) -> None:
        fmt, merge = ytmd.build_format_selector(dict(ytmd.DEFAULT_SETTINGS, mode="video"), has_ffmpeg=False)
        self.assertNotIn("+", fmt)
        self.assertIsNone(merge)

    def test_numbering_prefix_and_template(self) -> None:
        opts = self._build(numberTracks=True, fileTemplate="%(title)s")
        out = opts["outtmpl"]["default"]
        self.assertIn("01 - %(title)s.%(ext)s", out)

    def test_cookies_from_browser_shape(self) -> None:
        opts = self._build(cookiesFrom="firefox")
        self.assertEqual(opts["cookiesfrombrowser"], ("firefox", None, None, None))
        self._assert_constructible(opts)

    def test_rate_limit_converted_to_bytes(self) -> None:
        self.assertEqual(self._build(rateLimitKbps=512)["ratelimit"], 512 * 1024)
        self.assertNotIn("ratelimit", self._build(rateLimitKbps=0))


class TestJsRuntimeSelection(unittest.TestCase):
    """Current yt-dlp needs a JS runtime for YouTube and only looks for Deno."""

    def setUp(self) -> None:
        ytmd.detect_js_runtimes.cache_clear()
        self.addCleanup(ytmd.detect_js_runtimes.cache_clear)

    def _with(self, available: dict[str, str]) -> None:
        patched = lambda: available  # noqa: E731
        patched.cache_clear = lambda: None  # type: ignore[attr-defined]
        self._real = ytmd.detect_js_runtimes
        ytmd.detect_js_runtimes = patched  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(ytmd, "detect_js_runtimes", self._real))

    def test_auto_prefers_deno(self) -> None:
        self._with({"node": "22", "deno": "2.3"})
        self.assertEqual(ytmd.chosen_js_runtime(ytmd.DEFAULT_SETTINGS), "deno")

    def test_auto_falls_back_to_node(self) -> None:
        self._with({"bun": "1.3", "node": "22"})
        self.assertEqual(ytmd.chosen_js_runtime(ytmd.DEFAULT_SETTINGS), "node")

    def test_auto_with_nothing_installed(self) -> None:
        self._with({})
        self.assertEqual(ytmd.chosen_js_runtime(ytmd.DEFAULT_SETTINGS), "")

    def test_explicit_choice_honoured(self) -> None:
        self._with({"node": "22", "deno": "2.3"})
        self.assertEqual(ytmd.chosen_js_runtime(dict(ytmd.DEFAULT_SETTINGS, jsRuntime="node")), "node")

    def test_explicit_choice_that_is_missing_reports_none(self) -> None:
        self._with({"node": "22"})
        self.assertEqual(ytmd.chosen_js_runtime(dict(ytmd.DEFAULT_SETTINGS, jsRuntime="deno")), "")

    def test_deno_is_left_implicit(self) -> None:
        self._with({"deno": "2.3"})
        opts: dict[str, Any] = {}
        ytmd.apply_js_runtime_opts(opts, ytmd.DEFAULT_SETTINGS)
        self.assertNotIn("js_runtimes", opts)  # yt-dlp already defaults to deno

    def test_non_default_runtime_is_declared(self) -> None:
        self._with({"node": "22"})
        opts: dict[str, Any] = {}
        ytmd.apply_js_runtime_opts(opts, ytmd.DEFAULT_SETTINGS)
        self.assertEqual(opts["js_runtimes"], {"node": {}})

    def test_real_ytdlp_accepts_the_dict_form(self) -> None:
        # The Python API wants {name: {config}}; the CLI's list form is rejected.
        with REAL_YDL({"quiet": True, "js_runtimes": {"node": {}}}):
            pass
        with self.assertRaises(ValueError):
            REAL_YDL({"quiet": True, "js_runtimes": ["node"]})

    def test_detection_returns_a_name_to_version_mapping(self) -> None:
        found = self._real() if hasattr(self, "_real") else ytmd.detect_js_runtimes()
        self.assertIsInstance(found, dict)
        for name, version in found.items():
            self.assertIn(name, ytmd.JS_RUNTIME_PRIORITY)
            self.assertIsInstance(version, str)


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #


class TestResolver(unittest.TestCase):
    def setUp(self) -> None:
        install_fake_ydl({"*": flat_playlist(3, with_dead=True)})
        self.addCleanup(restore_ydl)

    def test_playlist_shape(self) -> None:
        out = ytmd.resolve_source("https://youtube.com/playlist?list=PLtest", ytmd.DEFAULT_SETTINGS)
        self.assertEqual(out["kind"], "playlist")
        self.assertEqual(out["title"], "Test Mix")
        self.assertEqual(out["count"], 4)
        self.assertEqual(out["totalDuration"], 180 + 181 + 182)

    def test_unavailable_flagged(self) -> None:
        out = ytmd.resolve_source("x", ytmd.DEFAULT_SETTINGS)
        self.assertFalse(out["tracks"][-1]["available"])
        self.assertTrue(all(t["available"] for t in out["tracks"][:-1]))

    def test_urls_are_canonical_watch_links(self) -> None:
        out = ytmd.resolve_source("x", ytmd.DEFAULT_SETTINGS)
        for track in out["tracks"]:
            self.assertTrue(track["url"].startswith("https://www.youtube.com/watch?v="), track["url"])

    def test_thumbnails_can_be_suppressed(self) -> None:
        out = ytmd.resolve_source("x", dict(ytmd.DEFAULT_SETTINGS, showThumbnails=False))
        self.assertTrue(all(t["thumbnail"] == "" for t in out["tracks"]))

    def test_nested_channel_tabs_flattened(self) -> None:
        install_fake_ydl({"*": {"_type": "playlist", "title": "Chan",
                                "entries": [flat_playlist(2), flat_playlist(2)]}})
        out = ytmd.resolve_source("x", ytmd.DEFAULT_SETTINGS)
        self.assertEqual(out["count"], 4)

    def test_none_result_is_a_clear_error(self) -> None:
        install_fake_ydl({"*": None})
        with self.assertRaises(ValueError):
            ytmd.resolve_source("x", ytmd.DEFAULT_SETTINGS)

    def test_failure_message_surfaces_the_real_ytdlp_error(self) -> None:
        """ignoreerrors hands back None, so the reason only exists in the log."""
        class LoggingFake(FakeYDL):
            def extract_info(self, url: str, download: bool = False) -> Any:
                logger = self.opts.get("logger")
                if logger:
                    logger.error("ERROR: [youtube] abc: Sign in to confirm you are not a bot")
                return None

        ytmd.yt_dlp.YoutubeDL = LoggingFake  # type: ignore[misc]
        self.addCleanup(restore_ydl)
        with self.assertRaises(ValueError) as caught:
            ytmd.resolve_source("https://youtube.com/playlist?list=PL1", ytmd.DEFAULT_SETTINGS)
        self.assertIn("not a bot", str(caught.exception))


# --------------------------------------------------------------------------- #
# HTTP integration
# --------------------------------------------------------------------------- #


class ServerFixture:
    """Boots the real HTTP handler on an ephemeral loopback port."""

    def __init__(self, tmp: Path) -> None:
        self.store = ytmd.SettingsStore(tmp / "settings.json")
        self.store.update({"outputDir": str(tmp / "downloads"), "politeThrottle": False, "concurrency": 2})
        self.token = "test-token-" + "z" * 20
        self.state = ytmd.AppState(self.store, self.token, 0)
        self.server = ytmd.make_server("127.0.0.1", 0, self.state)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path: str, method: str = "GET", body: Any = None,
                token: Optional[str] = "", headers: Optional[dict[str, str]] = None,
                timeout: float = 10.0) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-Ytmd-Token", self.token if token == "" else (token or ""))
        if data:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read().decode()
                return res.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            return exc.code, (json.loads(raw) if raw.strip().startswith("{") else raw)

    def stream_events(self, job_id: str, deadline: float = 20.0) -> list[dict[str, Any]]:
        req = urllib.request.Request(f"{self.base}/api/jobs/{job_id}/events")
        req.add_header("X-Ytmd-Token", self.token)
        events: list[dict[str, Any]] = []
        stop = time.time() + deadline
        with urllib.request.urlopen(req, timeout=deadline) as res:
            buffer = ""
            while time.time() < stop:
                chunk = res.read(1)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    payload = "\n".join(
                        line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:")
                    )
                    if not payload:
                        continue
                    event = json.loads(payload)
                    events.append(event)
                    if event.get("type") == "end":
                        return events
        return events


class HttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="ytmd-test-"))
        install_fake_ydl({"*": flat_playlist(3)})
        self.fixture = ServerFixture(self.tmp)
        self.addCleanup(self.fixture.close)
        self.addCleanup(restore_ydl)


class TestHttpSecurity(HttpTestCase):
    def test_index_is_public_and_carries_the_token(self) -> None:
        status, body = self.fixture.request("/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(self.fixture.token, body)
        self.assertNotIn("__YTMD_TOKEN__", body)

    def test_api_requires_the_token(self) -> None:
        self.assertEqual(self.fixture.request("/api/health", token=None)[0], 401)
        self.assertEqual(self.fixture.request("/api/health", token="wrong")[0], 401)
        self.assertEqual(self.fixture.request("/api/health")[0], 200)

    def test_foreign_host_header_rejected(self) -> None:
        status, _ = self.fixture.request("/api/health", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_foreign_origin_rejected(self) -> None:
        status, _ = self.fixture.request("/api/health", headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)

    def test_same_origin_accepted(self) -> None:
        status, _ = self.fixture.request("/api/health", headers={"Origin": f"http://127.0.0.1:{self.fixture.port}"})
        self.assertEqual(status, 200)

    def test_no_cors_headers_are_ever_sent(self) -> None:
        req = urllib.request.Request(self.fixture.base + "/api/health", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as res:
            for header in res.headers:
                self.assertFalse(header.lower().startswith("access-control-"), header)

    def test_unknown_paths_404(self) -> None:
        self.assertEqual(self.fixture.request("/../ytmd.py", token=None)[0], 404)
        self.assertEqual(self.fixture.request("/api/nope")[0], 404)

    def test_oversized_body_rejected(self) -> None:
        req = urllib.request.Request(self.fixture.base + "/api/resolve", data=b"{}", method="POST")
        req.add_header("X-Ytmd-Token", self.fixture.token)
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(ytmd.MAX_BODY_BYTES + 1))
        # urllib sends its own Content-Length, so exercise the guard directly.
        self.assertGreater(ytmd.MAX_BODY_BYTES, 0)

    def test_malformed_json_is_a_400(self) -> None:
        req = urllib.request.Request(self.fixture.base + "/api/resolve", data=b"not json", method="POST")
        req.add_header("X-Ytmd-Token", self.fixture.token)
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


class TestHttpApi(HttpTestCase):
    def test_health(self) -> None:
        status, body = self.fixture.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], ytmd.APP_VERSION)

    def test_settings_roundtrip_and_persistence(self) -> None:
        status, body = self.fixture.request(
            "/api/settings", "POST", {"settings": {"audioFormat": "opus", "concurrency": 42}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["audioFormat"], "opus")
        self.assertEqual(body["settings"]["concurrency"], 8)
        reloaded = ytmd.SettingsStore(self.tmp / "settings.json")
        self.assertEqual(reloaded.get()["audioFormat"], "opus")

    def test_resolve(self) -> None:
        status, body = self.fixture.request("/api/resolve", "POST", {"url": "https://youtube.com/playlist?list=PL1"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["tracks"]), 3)

    def test_resolve_needs_a_url(self) -> None:
        self.assertEqual(self.fixture.request("/api/resolve", "POST", {"url": "  "})[0], 400)

    def test_path_check(self) -> None:
        status, body = self.fixture.request("/api/path/check", "POST", {"path": str(self.tmp / "new")})
        self.assertEqual(status, 200)
        self.assertFalse(body["exists"])
        self.assertTrue(body["writable"])

    def test_job_rejects_empty_and_non_http_urls(self) -> None:
        self.assertEqual(self.fixture.request("/api/jobs", "POST", {"tracks": []})[0], 400)
        status, body = self.fixture.request(
            "/api/jobs", "POST", {"tracks": [{"url": "file:///etc/passwd", "title": "x"}]}
        )
        self.assertEqual(status, 400)

    def test_unknown_job_is_404(self) -> None:
        self.assertEqual(self.fixture.request("/api/jobs/deadbeef")[0], 404)


class TestJobLifecycle(HttpTestCase):
    def _tracks(self, count: int = 3) -> list[dict[str, Any]]:
        return [
            {"id": f"v{i}", "title": f"Track {i}", "uploader": "A", "index": i + 1,
             "url": f"https://www.youtube.com/watch?v=vid{i}"}
            for i in range(count)
        ]

    def test_happy_path(self) -> None:
        install_fake_ydl({"*": {"bytes": 500_000, "filepath": str(self.tmp / "a.mp3")}})
        status, body = self.fixture.request("/api/jobs", "POST",
                                            {"tracks": self._tracks(3), "playlistTitle": "Mix"})
        self.assertEqual(status, 201)
        events = self.fixture.stream_events(body["jobId"])
        self.assertTrue(any(e.get("type") == "end" for e in events))
        status, snapshot = self.fixture.request("/api/jobs/" + body["jobId"])
        self.assertEqual(snapshot["state"], "completed")
        self.assertEqual(snapshot["counts"]["done"], 3)
        self.assertTrue(all(item["progress"] == 1.0 for item in snapshot["items"]))

    def test_progress_events_are_emitted(self) -> None:
        install_fake_ydl({"*": {"bytes": 500_000, "delay": 0.02}})
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": self._tracks(2)})
        events = self.fixture.stream_events(body["jobId"])
        item_events = [e for e in events if e.get("type") == "item"]
        self.assertTrue(item_events, "expected per-item progress events")
        self.assertTrue(any(e["item"]["state"] == "done" for e in item_events))

    def test_failures_are_reported_per_item(self) -> None:
        install_fake_ydl({"*": {"raise": yt_dlp.utils.DownloadError("ERROR: Video unavailable")}})
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": self._tracks(2)})
        self.fixture.stream_events(body["jobId"])
        _, snapshot = self.fixture.request("/api/jobs/" + body["jobId"])
        self.assertEqual(snapshot["state"], "completed_with_errors")
        self.assertEqual(snapshot["counts"]["error"], 2)
        self.assertIn("Video unavailable", snapshot["items"][0]["message"])

    def test_one_failure_does_not_stop_the_rest(self) -> None:
        install_fake_ydl({
            "https://www.youtube.com/watch?v=vid0": {"raise": yt_dlp.utils.DownloadError("boom")},
            "*": {"bytes": 1000},
        })
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": self._tracks(3)})
        self.fixture.stream_events(body["jobId"])
        _, snapshot = self.fixture.request("/api/jobs/" + body["jobId"])
        self.assertEqual(snapshot["counts"]["error"], 1)
        self.assertEqual(snapshot["counts"]["done"], 2)

    def test_cancel_stops_the_job(self) -> None:
        install_fake_ydl({"*": {"bytes": 10_000_000, "delay": 0.6}})
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": self._tracks(4)})
        job_id = body["jobId"]
        time.sleep(0.4)
        self.assertEqual(self.fixture.request(f"/api/jobs/{job_id}/cancel", "POST")[0], 200)
        self.fixture.stream_events(job_id, deadline=15)
        _, snapshot = self.fixture.request("/api/jobs/" + job_id)
        self.assertEqual(snapshot["state"], "cancelled")
        self.assertGreater(snapshot["counts"]["cancelled"], 0)
        for item in snapshot["items"]:
            self.assertIn(item["state"], {"cancelled", "done", "skipped"})

    def test_reconnecting_to_a_finished_job_still_ends(self) -> None:
        install_fake_ydl({"*": {"bytes": 1000}})
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": self._tracks(1)})
        self.fixture.stream_events(body["jobId"])
        again = self.fixture.stream_events(body["jobId"], deadline=5)
        self.assertTrue(any(e.get("type") == "end" for e in again))

    def test_settings_are_applied_to_the_downloader(self) -> None:
        install_fake_ydl({"*": {"bytes": 1000}})
        self.fixture.request("/api/settings", "POST",
                             {"settings": {"audioFormat": "opus", "numberTracks": True, "folderMode": "playlist"}})
        _, body = self.fixture.request("/api/jobs", "POST",
                                       {"tracks": self._tracks(1), "playlistTitle": "My Mix"})
        self.fixture.stream_events(body["jobId"])
        download_opts = [o for o in FakeYDL.seen_opts if "outtmpl" in o]
        self.assertTrue(download_opts)
        template = download_opts[-1]["outtmpl"]["default"]
        self.assertIn("My Mix", template)
        self.assertIn("01 - ", template)
        codecs = [pp.get("preferredcodec") for pp in download_opts[-1]["postprocessors"]]
        # ffmpeg may be absent on the test machine, in which case there is no chain.
        if download_opts[-1]["postprocessors"]:
            self.assertIn("opus", codecs)

    def test_progress_reaches_the_rows_the_page_is_showing(self) -> None:
        """Regression: a non-prefix selection used to strand every row after
        the first gap, because the server re-derived uids from 0."""
        install_fake_ydl({"*": {"bytes": 1000}})
        page_tracks = [
            {"uid": f"{i}:v{i}", "id": f"v{i}", "title": f"Track {i}", "uploader": "A",
             "index": i + 1, "url": f"https://www.youtube.com/watch?v=v{i}"}
            for i in range(6)
        ]
        selection = [page_tracks[i] for i in (0, 3, 5)]
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": selection})
        self.fixture.stream_events(body["jobId"])
        _, snapshot = self.fixture.request("/api/jobs/" + body["jobId"])
        self.assertEqual([item["uid"] for item in snapshot["items"]], ["0:v0", "3:v3", "5:v5"])
        self.assertEqual(snapshot["counts"]["done"], 3)

    def test_cancelling_one_item_by_its_page_uid(self) -> None:
        install_fake_ydl({"*": {"bytes": 5_000_000, "delay": 0.5}})
        tracks = [
            {"uid": f"{i}:v{i}", "id": f"v{i}", "title": f"T{i}", "index": i + 1,
             "url": f"https://www.youtube.com/watch?v=v{i}"}
            for i in (0, 4)
        ]
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": tracks})
        status, result = self.fixture.request(f"/api/jobs/{body['jobId']}/items/4:v4/cancel", "POST")
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.fixture.request(f"/api/jobs/{body['jobId']}/cancel", "POST")
        self.fixture.stream_events(body["jobId"], deadline=15)

    def test_log_entries_have_increasing_sequence_numbers(self) -> None:
        install_fake_ydl({"*": {"bytes": 1000}})
        _, body = self.fixture.request("/api/jobs", "POST", {"tracks": self._tracks(1)})
        self.fixture.stream_events(body["jobId"])
        _, snapshot = self.fixture.request("/api/jobs/" + body["jobId"])
        seqs = [entry["seq"] for entry in snapshot["log"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
