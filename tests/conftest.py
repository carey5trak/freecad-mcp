from __future__ import annotations

import pytest

import _fakes


@pytest.fixture
def fake_yt_dlp(monkeypatch: pytest.MonkeyPatch):
    """Replace yt-dlp with a fake for the duration of a test."""
    return _fakes.install(monkeypatch)
