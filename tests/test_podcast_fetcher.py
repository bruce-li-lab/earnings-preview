"""Tests for podcast feed fetching safety guards."""

from __future__ import annotations

import pytest

from podcast_takeaways.fetcher import parse_feed


class _FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        is_redirect: bool = False,
    ) -> None:
        self.url = url
        self._content = content
        self.headers = headers or {}
        self.is_redirect = is_redirect
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def close(self) -> None:
        self.closed = True


def test_parse_feed_rejects_local_url() -> None:
    with pytest.raises(ValueError, match="Unsafe RSS URL"):
        parse_feed("http://localhost/feed.xml")


def test_parse_feed_rejects_redirect_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args, **kwargs) -> _FakeResponse:
        return _FakeResponse(
            url="https://example.com/feed.xml",
            headers={"location": "http://127.0.0.1/feed.xml"},
            is_redirect=True,
        )

    monkeypatch.setattr("podcast_takeaways.fetcher.requests.get", fake_get)

    with pytest.raises(ValueError, match="Unsafe redirect target"):
        parse_feed("https://example.com/feed.xml")


def test_parse_feed_parses_guarded_feed_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    rss = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>Episode One</title>
      <pubDate>Tue, 26 May 2026 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg" />
    </item>
  </channel>
</rss>
"""

    def fake_get(*args, **kwargs) -> _FakeResponse:
        return _FakeResponse(
            url="https://example.com/feed.xml",
            content=rss,
            headers={"content-length": str(len(rss))},
        )

    monkeypatch.setattr("podcast_takeaways.fetcher.requests.get", fake_get)

    episodes = parse_feed("https://example.com/feed.xml")

    assert len(episodes) == 1
    assert episodes[0].title == "Episode One"
    assert episodes[0].url == "https://cdn.example.com/ep1.mp3"
