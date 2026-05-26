"""Tests for the news disk cache."""

from __future__ import annotations

from datetime import date

import pytest

from earnings_analyzer import cache
from earnings_analyzer.news_sources_types import NewsItem


def test_cache_put_and_get_round_trip_for_past_date(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    target_date = date(2026, 1, 2)
    items = [
        NewsItem(
            title="Example",
            url="https://example.com/story",
            source="Techmeme",
            summary="A cached story",
            topics=["ai"],
            metadata={"score": 10},
        )
    ]

    cache.cache_put("techmeme", target_date, items)
    cached = cache.cache_get("techmeme", target_date)

    assert cached is not None
    assert len(cached) == 1
    assert cached[0].title == "Example"
    assert cached[0].url == "https://example.com/story"
    assert cached[0].topics == ["ai"]
    assert cached[0].metadata == {"score": 10}


def test_cache_put_skips_today_and_future_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    target_date = date.today()

    cache.cache_put(
        "techmeme",
        target_date,
        [NewsItem(title="Today", url="https://example.com/today", source="Techmeme")],
    )

    assert cache.cache_get("techmeme", target_date) is None
    assert not any(tmp_path.rglob("*.json"))


def test_cache_rejects_unsafe_source_names(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)

    with pytest.raises(ValueError, match="Unsafe cache source name"):
        cache.cache_get("../bad", date(2026, 1, 2))
