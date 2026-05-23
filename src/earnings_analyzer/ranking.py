"""Ranking and topic selection for the daily news pipeline."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from earnings_analyzer.news_sources_types import DailyNewsSources, NewsItem

DEFAULT_TOPIC_PROFILES: dict[str, list[str]] = {
    "ai": [
        "ai",
        "artificial intelligence",
        "agent",
        "anthropic",
        "claude",
        "deepmind",
        "gemini",
        "gpt",
        "inference",
        "llama",
        "llm",
        "model",
        "openai",
        "transformer",
    ],
    "markets": [
        "earnings",
        "fed",
        "guidance",
        "inflation",
        "ipo",
        "margin",
        "market",
        "nasdaq",
        "rate",
        "revenue",
        "sec",
        "stock",
    ],
    "policy": [
        "antitrust",
        "ban",
        "bill",
        "china",
        "congress",
        "eu",
        "export control",
        "lawsuit",
        "regulation",
        "regulator",
    ],
    "research": [
        "arxiv",
        "benchmark",
        "dataset",
        "paper",
        "research",
        "robot",
        "training",
    ],
}

DEFAULT_SOURCE_WEIGHTS: dict[str, float] = {
    "Techmeme": 7.0,
    "Financial Times": 6.5,
    "FT": 6.5,
    "SEC EDGAR": 6.0,
    "X (via cross-post)": 5.8,
    "Hacker News": 5.2,
    "HF Papers": 4.8,
    "ArXiv": 4.2,
    "Official": 6.8,
    "Spotify": 3.5,
    "Reddit": 3.2,
    "r/": 3.2,
    "@": 2.8,
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "for",
    "from",
    "in",
    "is",
    "it",
    "new",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def iter_news_items(news: DailyNewsSources) -> list[NewsItem]:
    """Return all collected source items in display-order buckets."""
    return [
        *news.techmeme_headlines,
        *news.viral_tweets,
        *news.official_updates,
        *news.ft_links,
        *news.hacker_news,
        *news.reddit_finance,
        *news.sec_filings,
        *news.x_links,
        *news.spotify_links,
        *news.arxiv_papers,
        *news.hf_papers,
    ]


def rank_daily_news(
    news: DailyNewsSources,
    *,
    active_topics: list[str] | None = None,
    topic_profiles: dict[str, list[str]] | None = None,
    source_weights: dict[str, float] | None = None,
    max_items: int = 12,
) -> list[NewsItem]:
    """Score and return a cross-source ranked list of the day's top items."""
    profiles = {**DEFAULT_TOPIC_PROFILES}
    if topic_profiles:
        profiles.update(topic_profiles)
    topics = active_topics or list(profiles)
    weights = {**DEFAULT_SOURCE_WEIGHTS}
    if source_weights:
        weights.update(source_weights)

    items = iter_news_items(news)
    if not items:
        return []

    url_groups: dict[str, list[NewsItem]] = defaultdict(list)
    title_groups: dict[str, list[NewsItem]] = defaultdict(list)
    for item in items:
        url_groups[_canonical_url_key(item.url)].append(item)
        title_groups[_title_key(item.title)].append(item)

    for item in items:
        matched_topics = _match_topics(item, profiles, topics)
        source_group_count = _source_group_count(item, url_groups, title_groups)
        score, reasons = _score_item(item, matched_topics, source_group_count, weights)
        item.score = round(score, 2)
        item.topics = matched_topics
        item.rank_reason = "; ".join(reasons[:4])
        item.metadata["cross_source_count"] = source_group_count

    return sorted(
        items,
        key=lambda item: (item.score, _metadata_number(item, "comments"), item.title),
        reverse=True,
    )[:max_items]


def _score_item(
    item: NewsItem,
    matched_topics: list[str],
    source_group_count: int,
    source_weights: dict[str, float],
) -> tuple[float, list[str]]:
    score = _source_weight(item.source, source_weights)
    reasons = [f"{item.source} source weight"]

    if matched_topics:
        topic_bonus = min(4.0, 1.5 * len(matched_topics))
        score += topic_bonus
        reasons.append("topics: " + ", ".join(matched_topics))

    if source_group_count > 1:
        cross_bonus = min(6.0, 2.2 * (source_group_count - 1))
        score += cross_bonus
        reasons.append(f"seen in {source_group_count} sources")

    engagement = _engagement_bonus(item)
    if engagement:
        score += engagement
        reasons.append("high engagement")

    recency = _recency_bonus(item)
    if recency:
        score += recency
        reasons.append("fresh")

    if item.source == "SEC EDGAR" and _looks_material_sec(item):
        score += 2.0
        reasons.append("material filing")

    if item.source == "X (via cross-post)":
        score += 1.5
        reasons.append("escaped X")

    return score, reasons


def _match_topics(
    item: NewsItem,
    profiles: dict[str, list[str]],
    active_topics: list[str],
) -> list[str]:
    haystack = f"{item.title} {item.summary} {item.source}".lower()
    matched: list[str] = []
    for topic in active_topics:
        keywords = profiles.get(topic, [])
        if any(keyword.lower() in haystack for keyword in keywords):
            matched.append(topic)
    return matched


def _source_weight(source: str, weights: dict[str, float]) -> float:
    if source in weights:
        return weights[source]
    for prefix, weight in weights.items():
        if source.startswith(prefix):
            return weight
    return 2.0


def _engagement_bonus(item: NewsItem) -> float:
    score = _metadata_number(item, "score") or _metadata_number(item, "upvotes")
    comments = _metadata_number(item, "comments")
    bonus = 0.0
    if score:
        bonus += min(4.0, math.log10(score + 1) * 1.4)
    if comments:
        bonus += min(2.5, math.log10(comments + 1) * 1.1)
    return round(bonus, 2)


def _metadata_number(item: NewsItem, key: str) -> float:
    raw = item.metadata.get(key)
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    return 0.0


def _recency_bonus(item: NewsItem) -> float:
    if not item.published_at:
        return 0.0
    raw = item.published_at.replace("Z", "+00:00")
    try:
        published = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600
    if age_hours < 0 or age_hours > 72:
        return 0.0
    return round(max(0.0, 2.0 - age_hours / 24), 2)


def _looks_material_sec(item: NewsItem) -> bool:
    haystack = f"{item.title} {item.summary}".lower()
    return any(
        term in haystack
        for term in (
            "2.02",
            "7.01",
            "8.01",
            "earnings",
            "guidance",
            "results",
            "10-q",
            "10-k",
            "8-k",
        )
    )


def _canonical_url_key(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if host in {"x.com", "twitter.com"}:
        match = re.search(r"/status/(\d+)", path)
        if match:
            return f"x-status:{match.group(1)}"
    return f"{host}{path}".lower()


def _title_key(title: str) -> str:
    tokens = [t for t in _TOKEN_RE.findall(title.lower()) if t not in _STOPWORDS]
    return " ".join(tokens[:10])


def _source_group_count(
    item: NewsItem,
    url_groups: dict[str, list[NewsItem]],
    title_groups: dict[str, list[NewsItem]],
) -> int:
    sources = {
        group_item.source
        for group_item in url_groups.get(_canonical_url_key(item.url), [])
    }
    sources.update(
        group_item.source
        for group_item in title_groups.get(_title_key(item.title), [])
    )
    return max(1, len(sources))
