"""Per-source, per-date disk cache for news fetches.

Past-date results are immutable, so we cache them forever. Today's results
are not cached (they evolve through the day). Cache is keyed by source name
and ISO date, stored as JSON for human inspection.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from earnings_analyzer.news_sources_types import NewsItem
from earnings_analyzer.url_security import sanitize_url


_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".cache" / "news"

_SAFE_NAME = re.compile(r"^[a-z0-9_-]+$")


def _cache_path(source: str, target_date: date) -> Path:
    if not _SAFE_NAME.match(source):
        raise ValueError(f"Unsafe cache source name: {source}")
    iso = target_date.isoformat()
    return _CACHE_DIR / source / f"{iso}.json"


def cache_get(source: str, target_date: date) -> list[NewsItem] | None:
    """Return cached items for (source, date), or None if absent."""
    path = _cache_path(source, target_date)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    items: list[NewsItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        url = sanitize_url(str(entry.get("url", "")))
        if not url:
            continue
        items.append(
            NewsItem(
                title=str(entry.get("title", ""))[:300],
                url=url,
                source=str(entry.get("source", "")),
                summary=str(entry.get("summary", "")),
                published_at=str(entry.get("published_at", "")),
                score=float(entry.get("score", 0) or 0),
                topics=[
                    str(topic)
                    for topic in entry.get("topics", [])
                    if isinstance(topic, str)
                ],
                rank_reason=str(entry.get("rank_reason", "")),
                metadata=entry.get("metadata", {})
                if isinstance(entry.get("metadata"), dict)
                else {},
            )
        )
    return items


def cache_put(source: str, target_date: date, items: list[NewsItem]) -> None:
    """Write items for (source, date) only if date is strictly in the past.

    Today's data is volatile; we never cache it. Empty results are not
    cached either, so a transient fetch failure doesn't poison the cache.
    """
    if target_date >= date.today():
        return
    if not items:
        return
    path = _cache_path(source, target_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = [
        {
            "title": it.title,
            "url": it.url,
            "source": it.source,
            "summary": it.summary,
            "published_at": it.published_at,
            "score": it.score,
            "topics": it.topics,
            "rank_reason": it.rank_reason,
            "metadata": it.metadata,
        }
        for it in items
    ]
    path.write_text(json.dumps(serialized, indent=2, ensure_ascii=False), encoding="utf-8")
