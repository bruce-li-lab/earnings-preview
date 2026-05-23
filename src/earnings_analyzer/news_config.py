"""User-customizable configuration for the daily news summary.

Drop a ``news_config.json`` file next to your working directory (or point
to one with ``--config``) to override defaults.  The schema is:

{
  "techmeme_count": 5,
  "hn_count": 5,
  "reddit_count": 10,
  "sec_count": 10,
  "output_dir": "./newsletters",
  "x_accounts": ["OpenAI", "AnthropicAI", "GoogleDeepMind"],
  "ft_sections": [
    {"title": "Markets", "url": "https://www.ft.com/markets", "summary": "..."}
  ],
  "spotify_podcasts": [
    {"title": "Podcast Name", "url": "https://open.spotify.com/show/...", "summary": "..."}
  ],
  "official_feeds": [
    {"title": "OpenAI News", "url": "https://openai.com/news/rss.xml"}
  ],
  "reddit_subreddits": ["investing", "stocks"],
  "reddit_sort": "top",
  "sec_form_types": ["8-K", "10-Q"],
  "active_topics": ["ai", "markets", "policy", "research"],
  "topic_profiles": {"semis": ["nvidia", "gpu", "tsmc"]},
  "source_weights": {"Techmeme": 7.0, "Official": 6.8}
}
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from earnings_analyzer.url_security import sanitize_url


def _validate_url(url: str) -> None:
    """Raise ValueError if *url* is not a valid http(s) URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not sanitize_url(url):
        raise ValueError(f"Invalid URL (must be http/https): {url}")


def _validate_link_list(items: list | None, label: str) -> None:
    """Validate a list of {title, url, ...} dicts from config."""
    if items is None:
        return
    if not isinstance(items, list):
        raise ValueError(f"{label} must be a list")
    for i, entry in enumerate(items):
        if not isinstance(entry, dict):
            raise ValueError(f"{label}[{i}] must be a dict")
        if "title" not in entry or "url" not in entry:
            raise ValueError(f"{label}[{i}] must have 'title' and 'url'")
        _validate_url(entry["url"])


@dataclass
class NewsConfig:
    """Resolved news summary configuration."""

    techmeme_count: int = 5
    hn_count: int = 5
    reddit_count: int = 5
    sec_count: int = 5
    arxiv_count: int = 5
    hf_count: int = 5
    output_dir: str = "./newsletters"
    x_accounts: list[str] | None = None
    x_bearer_token: str | None = None
    ft_sections: list[dict[str, str]] | None = None
    spotify_podcasts: list[dict[str, str]] | None = None
    official_feeds: list[dict[str, str]] | None = None
    reddit_subreddits: list[str] | None = None
    reddit_sort: str = "top"
    sec_form_types: list[str] | None = None
    sec_material_only: bool = False
    active_topics: list[str] | None = None
    topic_profiles: dict[str, list[str]] | None = None
    source_weights: dict[str, float] | None = None
    obsidian_vault: str | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> NewsConfig:
        """Load config from a JSON file, falling back to defaults."""
        if path is None:
            path = Path("news_config.json")
        else:
            path = Path(path)

        if not path.exists():
            return cls()

        raw = path.read_text(encoding="utf-8")
        if len(raw) > 1_000_000:
            raise ValueError("Config file too large (>1 MB)")
        data = json.loads(raw)

        # --- validate scalars ---
        techmeme_count = data.get("techmeme_count", 5)
        if not isinstance(techmeme_count, int) or not 1 <= techmeme_count <= 100:
            raise ValueError(f"techmeme_count must be 1-100, got {techmeme_count}")

        hn_count = data.get("hn_count", 5)
        if not isinstance(hn_count, int) or not 1 <= hn_count <= 100:
            raise ValueError(f"hn_count must be 1-100, got {hn_count}")

        reddit_count = data.get("reddit_count", 5)
        if not isinstance(reddit_count, int) or not 1 <= reddit_count <= 100:
            raise ValueError(f"reddit_count must be 1-100, got {reddit_count}")

        sec_count = data.get("sec_count", 5)
        if not isinstance(sec_count, int) or not 1 <= sec_count <= 50:
            raise ValueError(f"sec_count must be 1-50, got {sec_count}")

        arxiv_count = data.get("arxiv_count", 5)
        if not isinstance(arxiv_count, int) or not 1 <= arxiv_count <= 50:
            raise ValueError(f"arxiv_count must be 1-50, got {arxiv_count}")

        hf_count = data.get("hf_count", 5)
        if not isinstance(hf_count, int) or not 1 <= hf_count <= 50:
            raise ValueError(f"hf_count must be 1-50, got {hf_count}")

        output_dir = data.get("output_dir", "./newsletters")
        if not isinstance(output_dir, str) or ".." in output_dir:
            raise ValueError(f"Invalid output_dir: {output_dir}")

        # --- validate x_accounts ---
        x_accounts = data.get("x_accounts")
        if x_accounts is not None:
            if not isinstance(x_accounts, list):
                raise ValueError("x_accounts must be a list")
            for acct in x_accounts:
                if not isinstance(acct, str) or not re.match(r"^[A-Za-z0-9_]{1,30}$", acct):
                    raise ValueError(f"Invalid X account handle: {acct}")

        # --- validate link lists ---
        _validate_link_list(data.get("ft_sections"), "ft_sections")
        _validate_link_list(data.get("spotify_podcasts"), "spotify_podcasts")
        _validate_link_list(data.get("official_feeds"), "official_feeds")

        # --- validate obsidian_vault ---
        obsidian_vault = data.get("obsidian_vault")
        if obsidian_vault is not None:
            if not isinstance(obsidian_vault, str):
                raise ValueError("obsidian_vault must be a string path")

        # --- validate string lists ---
        reddit_subs = data.get("reddit_subreddits")
        if reddit_subs is not None:
            if not isinstance(reddit_subs, list):
                raise ValueError("reddit_subreddits must be a list")
            for s in reddit_subs:
                if not isinstance(s, str) or not re.match(r"^[A-Za-z0-9_]{1,30}$", s):
                    raise ValueError(f"Invalid subreddit name: {s}")

        reddit_sort = data.get("reddit_sort", "top")
        if reddit_sort not in {"top", "hot", "rising"}:
            raise ValueError("reddit_sort must be one of: top, hot, rising")

        sec_forms = data.get("sec_form_types")
        if sec_forms is not None:
            if not isinstance(sec_forms, list):
                raise ValueError("sec_form_types must be a list")
            for f in sec_forms:
                if not isinstance(f, str) or not re.match(r"^[A-Za-z0-9\-/]{1,10}$", f):
                    raise ValueError(f"Invalid SEC form type: {f}")

        sec_material_only = data.get("sec_material_only", False)
        if not isinstance(sec_material_only, bool):
            raise ValueError("sec_material_only must be true or false")

        active_topics = data.get("active_topics")
        if active_topics is not None:
            if not isinstance(active_topics, list):
                raise ValueError("active_topics must be a list")
            for topic in active_topics:
                if not isinstance(topic, str) or not re.match(r"^[A-Za-z0-9_-]{1,40}$", topic):
                    raise ValueError(f"Invalid topic name: {topic}")

        topic_profiles = data.get("topic_profiles")
        if topic_profiles is not None:
            if not isinstance(topic_profiles, dict):
                raise ValueError("topic_profiles must be an object")
            for topic, keywords in topic_profiles.items():
                if not isinstance(topic, str) or not re.match(r"^[A-Za-z0-9_-]{1,40}$", topic):
                    raise ValueError(f"Invalid topic profile name: {topic}")
                if not isinstance(keywords, list) or not all(
                    isinstance(k, str) and 1 <= len(k) <= 80 for k in keywords
                ):
                    raise ValueError(f"topic_profiles.{topic} must be a list of strings")

        source_weights_raw = data.get("source_weights")
        source_weights: dict[str, float] | None = None
        if source_weights_raw is not None:
            if not isinstance(source_weights_raw, dict):
                raise ValueError("source_weights must be an object")
            source_weights = {}
            for source, weight in source_weights_raw.items():
                if not isinstance(source, str) or not source or len(source) > 80:
                    raise ValueError(f"Invalid source weight key: {source}")
                if not isinstance(weight, (int, float)) or not 0 <= float(weight) <= 20:
                    raise ValueError(f"Invalid source weight for {source}: {weight}")
                source_weights[source] = float(weight)

        return cls(
            techmeme_count=techmeme_count,
            hn_count=hn_count,
            reddit_count=reddit_count,
            sec_count=sec_count,
            arxiv_count=arxiv_count,
            hf_count=hf_count,
            output_dir=output_dir,
            x_accounts=x_accounts,
            x_bearer_token=os.environ.get("X_BEARER_TOKEN") or data.get("x_bearer_token"),
            ft_sections=data.get("ft_sections"),
            spotify_podcasts=data.get("spotify_podcasts"),
            official_feeds=data.get("official_feeds"),
            reddit_subreddits=reddit_subs,
            reddit_sort=reddit_sort,
            sec_form_types=sec_forms,
            sec_material_only=sec_material_only,
            active_topics=active_topics,
            topic_profiles=topic_profiles,
            source_weights=source_weights,
            obsidian_vault=obsidian_vault,
        )
