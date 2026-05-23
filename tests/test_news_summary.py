"""Tests for the daily news summary pipeline."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from earnings_analyzer.news_sources import (
    DailyNewsSources,
    NewsItem,
    _extract_tweet_urls,
    _find_sharing_context,
    _sanitize_url,
    fetch_ft_articles,
    fetch_spotify_episodes,
    fetch_x_posts,
)
from earnings_analyzer.newsletter import render_newsletter, save_newsletter
from earnings_analyzer.news_config import NewsConfig
from earnings_analyzer.news_analyzer import _build_headlines_prompt, _fallback_summary
from earnings_analyzer.ranking import rank_daily_news


# ---------------------------------------------------------------------------
# URL sanitization
# ---------------------------------------------------------------------------


def test_sanitize_url_valid():
    assert _sanitize_url("https://example.com/foo") == "https://example.com/foo"
    assert _sanitize_url("http://example.com") == "http://example.com"


def test_sanitize_url_rejects_bad_schemes():
    assert _sanitize_url("javascript:alert(1)") is None
    assert _sanitize_url("ftp://example.com") is None
    assert _sanitize_url("file:///etc/passwd") is None
    assert _sanitize_url("data:text/html,<h1>hi</h1>") is None


def test_sanitize_url_rejects_empty():
    assert _sanitize_url("") is None
    assert _sanitize_url("not-a-url") is None


def test_sanitize_url_rejects_local_targets():
    assert _sanitize_url("http://localhost:8000") is None
    assert _sanitize_url("http://127.0.0.1/admin") is None
    assert _sanitize_url("http://10.0.0.5/metadata") is None


# ---------------------------------------------------------------------------
# news_sources — curated link tests
# ---------------------------------------------------------------------------


def test_fetch_x_posts_rejects_bad_handle():
    # Invalid handles are silently skipped
    items = fetch_x_posts(accounts=["../bad!", "valid_handle"])
    # Should not crash; invalid handles filtered out
    assert isinstance(items, list)


def test_fetch_ft_articles_rejects_bad_url():
    custom = [{"title": "Bad", "url": "javascript:alert(1)"}]
    items = fetch_ft_articles(sections=custom)
    assert len(items) == 0


def test_fetch_spotify_episodes_rejects_bad_url():
    custom = [{"title": "Bad", "url": "ftp://evil.com"}]
    items = fetch_spotify_episodes(shows=custom)
    assert len(items) == 0


# ---------------------------------------------------------------------------
# newsletter rendering tests
# ---------------------------------------------------------------------------


def _make_sample_news() -> DailyNewsSources:
    return DailyNewsSources(
        date=date(2026, 4, 4),
        techmeme_headlines=[
            NewsItem(
                title="AI Startup Raises $1B",
                url="https://example.com/ai",
                source="Techmeme",
                summary="A major AI company secures funding.",
            ),
            NewsItem(
                title="Apple Announces New Chip",
                url="https://example.com/apple",
                source="Techmeme",
            ),
        ],
        hacker_news=[
            NewsItem(
                title="Show HN: Cool Project",
                url="https://example.com/hn",
                source="Hacker News",
                summary="Score: 200 | Comments: 50",
            ),
        ],
        reddit_finance=[
            NewsItem(
                title="Market rally continues",
                url="https://reddit.com/r/investing/post",
                source="r/investing",
                summary="Score: 500 | Comments: 120",
            ),
        ],
        sec_filings=[
            NewsItem(
                title="AAPL 8-K Filed",
                url="https://www.sec.gov/filing",
                source="SEC EDGAR",
            ),
        ],
        x_links=[
            NewsItem(title="OpenAI announces GPT-5", url="https://x.com/OpenAI/status/123", source="@OpenAI"),
        ],
        ft_links=[
            NewsItem(title="AI regulation in EU", url="https://www.ft.com/content/abc", source="FT Technology"),
        ],
        official_updates=[
            NewsItem(title="OpenAI publishes safety update", url="https://openai.com/news/example", source="Official OpenAI News"),
        ],
        spotify_links=[
            NewsItem(title="All-In: AI Wars", url="https://open.spotify.com/episode/abc", source="Spotify", summary="Discussion on AI competition."),
        ],
        analysis="## Top Stories\n- **AI funding** is the big story today.",
    )


def test_render_newsletter_contains_all_sections():
    html = render_newsletter(_make_sample_news())
    assert "Daily News Summary" in html
    assert "Saturday, April 4, 2026" in html
    assert "Techmeme Headlines" in html
    assert "Hacker News" in html
    assert "Reddit Finance" in html
    assert "SEC Filings" in html
    assert "X / Twitter" in html
    assert "Financial Times" in html
    assert "Official Updates" in html
    assert "Spotify Podcasts" in html
    assert "AI Intelligence Brief" in html


def test_render_newsletter_contains_items():
    html = render_newsletter(_make_sample_news())
    assert "AI Startup Raises $1B" in html
    assert "https://example.com/ai" in html
    assert "A major AI company secures funding." in html
    assert "Show HN: Cool Project" in html
    assert "Market rally continues" in html
    assert "AAPL 8-K Filed" in html


def test_render_newsletter_contains_analysis():
    html = render_newsletter(_make_sample_news())
    assert "Top Stories" in html
    assert "AI funding" in html


def test_render_newsletter_escapes_html():
    news = DailyNewsSources(
        date=date(2026, 1, 1),
        techmeme_headlines=[
            NewsItem(
                title='<script>alert("xss")</script>',
                url="https://example.com",
                source="Techmeme",
            )
        ],
    )
    html = render_newsletter(news)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_save_newsletter(tmp_path: Path):
    news = _make_sample_news()
    filepath = save_newsletter(news, output_dir=tmp_path)
    assert filepath.exists()
    assert filepath.name == "news-summary-2026-04-04.html"
    content = filepath.read_text(encoding="utf-8")
    assert "Daily News Summary" in content


def test_save_newsletter_custom_filename(tmp_path: Path):
    news = _make_sample_news()
    filepath = save_newsletter(news, output_dir=tmp_path, filename="custom.html")
    assert filepath.name == "custom.html"
    assert filepath.exists()


def test_save_newsletter_rejects_path_traversal(tmp_path: Path):
    news = _make_sample_news()
    with pytest.raises(ValueError, match="Invalid filename"):
        save_newsletter(news, output_dir=tmp_path, filename="../../etc/evil.html")


def test_save_newsletter_rejects_slash_in_filename(tmp_path: Path):
    news = _make_sample_news()
    with pytest.raises(ValueError, match="Invalid filename"):
        save_newsletter(news, output_dir=tmp_path, filename="sub/file.html")


# ---------------------------------------------------------------------------
# config tests
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = NewsConfig()
    assert cfg.techmeme_count == 5
    assert cfg.hn_count == 5
    assert cfg.reddit_count == 5
    assert cfg.sec_count == 5
    assert cfg.arxiv_count == 5
    assert cfg.hf_count == 5
    assert cfg.output_dir == "./newsletters"
    assert cfg.x_accounts is None
    assert cfg.obsidian_vault is None


def test_config_load_missing_file():
    cfg = NewsConfig.load("/nonexistent/path.json")
    assert cfg.techmeme_count == 5


def test_config_load_from_file(tmp_path: Path):
    config_file = tmp_path / "news_config.json"
    config_file.write_text(
        '{"techmeme_count": 5, "hn_count": 3, "output_dir": "./out",'
        ' "x_accounts": ["OpenAI", "AnthropicAI"]}'
    )
    cfg = NewsConfig.load(config_file)
    assert cfg.techmeme_count == 5
    assert cfg.hn_count == 3
    assert cfg.output_dir == "./out"
    assert cfg.x_accounts is not None
    assert len(cfg.x_accounts) == 2


def test_config_rejects_bad_techmeme_count(tmp_path: Path):
    config_file = tmp_path / "bad.json"
    config_file.write_text('{"techmeme_count": 999}')
    with pytest.raises(ValueError, match="techmeme_count"):
        NewsConfig.load(config_file)


def test_config_rejects_path_traversal_output_dir(tmp_path: Path):
    config_file = tmp_path / "bad.json"
    config_file.write_text('{"output_dir": "../../etc"}')
    with pytest.raises(ValueError, match="output_dir"):
        NewsConfig.load(config_file)


def test_config_rejects_bad_x_account(tmp_path: Path):
    config_file = tmp_path / "bad.json"
    config_file.write_text('{"x_accounts": ["../../evil"]}')
    with pytest.raises(ValueError, match="Invalid X account"):
        NewsConfig.load(config_file)


def test_config_rejects_bad_subreddit(tmp_path: Path):
    config_file = tmp_path / "bad.json"
    config_file.write_text('{"reddit_subreddits": ["valid", "../../bad"]}')
    with pytest.raises(ValueError, match="Invalid subreddit"):
        NewsConfig.load(config_file)


# ---------------------------------------------------------------------------
# analysis tests
# ---------------------------------------------------------------------------


def test_fallback_summary():
    news = _make_sample_news()
    summary = _fallback_summary(news)
    assert "Daily Brief" in summary
    assert "Techmeme" in summary
    assert "Hacker News" in summary
    assert "AI Startup Raises $1B" in summary
    assert "GEMINI_API_KEY" in summary


def test_build_headlines_prompt():
    news = _make_sample_news()
    news.ranked_items = rank_daily_news(news)[:3]
    prompt = _build_headlines_prompt(news)
    assert "2026-04-04" in prompt
    assert "Ranked Top Stories" in prompt
    assert "Techmeme" in prompt
    assert "Official Updates" in prompt
    assert "Spotify Podcasts" in prompt
    assert "Hacker News" in prompt
    assert "AI Startup Raises $1B" in prompt
    assert "intelligence brief" in prompt


def test_rank_daily_news_boosts_cross_source_and_engagement():
    news = DailyNewsSources(
        date=date(2026, 4, 4),
        techmeme_headlines=[
            NewsItem(
                title="OpenAI launches new agent platform",
                url="https://example.com/openai-agent",
                source="Techmeme",
            )
        ],
        hacker_news=[
            NewsItem(
                title="OpenAI launches new agent platform",
                url="https://news.ycombinator.com/item?id=1",
                source="Hacker News",
                metadata={"score": 500, "comments": 120},
            )
        ],
        reddit_finance=[
            NewsItem(
                title="Routine bank branch update",
                url="https://example.com/bank",
                source="r/finance",
                metadata={"score": 10, "comments": 2},
            )
        ],
    )

    ranked = rank_daily_news(news, active_topics=["ai"])
    assert ranked[0].title == "OpenAI launches new agent platform"
    assert ranked[0].score > ranked[-1].score
    assert "ai" in ranked[0].topics
    assert ranked[0].metadata["cross_source_count"] == 2


def test_fallback_summary_empty_sources():
    news = DailyNewsSources(date=date(2026, 1, 1))
    summary = _fallback_summary(news)
    assert "Daily Brief" in summary
    assert "GEMINI_API_KEY" in summary


# ---------------------------------------------------------------------------
# viral tweet extraction tests
# ---------------------------------------------------------------------------


def test_extract_tweet_urls_from_sources():
    sources = [
        [
            NewsItem(
                title="Article about a tweet",
                url="https://x.com/elonmusk/status/123456789",
                source="Techmeme",
            ),
            NewsItem(
                title="Normal article",
                url="https://example.com/article",
                source="Techmeme",
            ),
        ],
        [
            NewsItem(
                title="HN post linking a tweet",
                url="https://twitter.com/naval/status/987654321",
                source="Hacker News",
            ),
        ],
    ]
    urls = _extract_tweet_urls(sources)
    assert len(urls) == 2
    assert "https://x.com/elonmusk/status/123456789" in urls
    assert "https://x.com/naval/status/987654321" in urls


def test_extract_tweet_urls_deduplicates():
    sources = [
        [
            NewsItem(
                title="A",
                url="https://x.com/user/status/111",
                source="Techmeme",
            ),
        ],
        [
            NewsItem(
                title="B",
                url="https://twitter.com/user/status/111",
                source="HN",
            ),
        ],
    ]
    urls = _extract_tweet_urls(sources)
    assert len(urls) == 1


def test_extract_tweet_urls_empty_when_no_tweets():
    sources = [
        [
            NewsItem(title="A", url="https://example.com", source="Techmeme"),
        ],
    ]
    urls = _extract_tweet_urls(sources)
    assert urls == []


def test_find_sharing_context():
    tweet_url = "https://x.com/user/status/555"
    sources = [
        [
            NewsItem(
                title="Discussion about the tweet",
                url="https://x.com/user/status/555",
                source="Techmeme",
            ),
        ],
        [
            NewsItem(
                title="HN thread",
                url="https://news.ycombinator.com/item?id=99",
                source="Hacker News",
                summary="links to https://x.com/user/status/555",
            ),
        ],
    ]
    context = _find_sharing_context(tweet_url, sources)
    assert "Techmeme" in context
    assert "Hacker News" in context


def test_find_sharing_context_excludes_x_source():
    tweet_url = "https://x.com/user/status/555"
    sources = [
        [
            NewsItem(
                title="X topic",
                url="https://x.com/user/status/555",
                source="@someuser",
            ),
        ],
    ]
    context = _find_sharing_context(tweet_url, sources)
    assert context == ""


def test_render_newsletter_contains_viral_tweets():
    news = DailyNewsSources(
        date=date(2026, 4, 4),
        viral_tweets=[
            NewsItem(
                title="@someuser",
                url="https://x.com/someuser/status/123",
                source="X (via cross-post)",
                summary="Big news today | Shared on Techmeme: Article title",
            ),
        ],
    )
    html = render_newsletter(news)
    assert "Viral Tweets" in html
    assert "@someuser" in html
    assert "Big news today" in html
    assert "Shared on Techmeme" in html
