"""Fetch and curate news from multiple sources for the daily summary.

Each fetcher accepts an optional ``target_date``. When set to a past date,
the fetcher takes a historical path (where the upstream supports it) and
results are cached on disk so re-runs are cheap. Sources without a real
historical API return empty for past dates rather than polluting the day
with current data.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from defusedxml import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from earnings_analyzer.cache import cache_get, cache_put
from earnings_analyzer.news_sources_types import DailyNewsSources, NewsItem
from earnings_analyzer.ranking import rank_daily_news
from earnings_analyzer.url_security import safe_join_url, sanitize_url

__all__ = [
    "DailyNewsSources",
    "NewsItem",
    "fetch_techmeme_headlines",
    "fetch_hacker_news",
    "fetch_reddit_finance",
    "fetch_sec_filings",
    "fetch_x_posts",
    "fetch_arxiv_papers",
    "fetch_hf_papers",
    "fetch_ft_articles",
    "fetch_official_updates",
    "fetch_spotify_episodes",
    "fetch_viral_tweets",
    "gather_daily_news",
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (compatible; EarningsAnalyzer/0.1; +https://github.com)"
)

_MAX_RESPONSE_BYTES = 5_000_000  # 5 MB ceiling for any single fetch


def _resolve_date(target_date: date | None) -> date:
    return target_date or date.today()


def _is_past(target_date: date) -> bool:
    return target_date < date.today()


def _sanitize_url(url: str) -> str | None:
    """Validate and return a URL, or None if suspicious."""
    return sanitize_url(url)


def _safe_get(url: str, **kwargs: object) -> httpx.Response:
    """Wrapper around httpx.get with redirect, URL, and size guards."""
    current_url = sanitize_url(url, resolve_host=True)
    if not current_url:
        raise ValueError(f"Unsafe URL: {url}")

    headers = {"User-Agent": _USER_AGENT}
    headers.update(kwargs.pop("headers", {}) or {})
    timeout = kwargs.pop("timeout", 15)
    params = kwargs.pop("params", None)
    kwargs.pop("follow_redirects", None)

    for _ in range(6):
        resp = httpx.get(
            current_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            params=params,
            **kwargs,
        )
        params = None
        if resp.is_redirect:
            location = resp.headers.get("location", "")
            next_url = safe_join_url(str(resp.url), location, resolve_host=True)
            if not next_url:
                raise ValueError(f"Unsafe redirect target: {location}")
            current_url = next_url
            continue

        resp.raise_for_status()
        if not sanitize_url(str(resp.url), resolve_host=True):
            raise ValueError(f"Unsafe final URL: {resp.url}")
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise ValueError("Response too large")
        return resp

    raise ValueError("Too many redirects")


def _parse_feed_datetime(raw: str | None) -> str:
    """Parse common RSS/Atom date strings to ISO-8601 UTC when possible."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return raw[:40]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Techmeme — front page (live) + archive snapshots (historical)
# ---------------------------------------------------------------------------

_TECHMEME_URL = "https://www.techmeme.com/"


def _techmeme_archive_url(target_date: date) -> str:
    # Techmeme archive paths use /YYMMDD/hHHMM. h1200 is the mid-day snapshot.
    return f"https://www.techmeme.com/{target_date.strftime('%y%m%d')}/h1200"


def _parse_techmeme_html(html: str, max_items: int) -> list[NewsItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen_titles: set[str] = set()

    for cluster in soup.select(".clus"):
        link_tag = cluster.select_one("a.ourh")
        if link_tag is None:
            continue
        title = link_tag.get_text(strip=True)[:300]
        raw_url = link_tag.get("href", "")
        url = _sanitize_url(raw_url)
        if not title or not url or title in seen_titles:
            continue
        seen_titles.add(title)

        cite = cluster.select_one(".ii")
        summary = cite.get_text(strip=True)[:200] if cite else ""

        items.append(
            NewsItem(
                title=title,
                url=url,
                source="Techmeme",
                summary=summary,
                metadata={"source_rank": len(items) + 1},
            )
        )
        if len(items) >= max_items:
            break

    return items


def fetch_techmeme_headlines(
    max_items: int = 5,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Scrape top headlines from Techmeme. Past dates use the archive."""
    max_items = min(max(1, max_items), 100)
    target = _resolve_date(target_date)

    cached = cache_get("techmeme", target)
    if cached is not None:
        return cached[:max_items]

    url = _techmeme_archive_url(target) if _is_past(target) else _TECHMEME_URL
    try:
        resp = _safe_get(url)
    except (httpx.HTTPError, ValueError):
        return []

    items = _parse_techmeme_html(resp.text, max_items)
    cache_put("techmeme", target, items)
    return items


# ---------------------------------------------------------------------------
# Hacker News — Firebase top stories (live) + Algolia search (historical)
# ---------------------------------------------------------------------------

_HN_FIREBASE_API = "https://hacker-news.firebaseio.com/v0"
_HN_ALGOLIA_API = "https://hn.algolia.com/api/v1/search"


def _fetch_hn_live(max_items: int, min_score: int) -> list[NewsItem]:
    try:
        resp = _safe_get(f"{_HN_FIREBASE_API}/topstories.json")
        story_ids: list[int] = resp.json()[:max_items * 3]
    except (httpx.HTTPError, ValueError):
        return []

    items: list[NewsItem] = []
    for sid in story_ids:
        try:
            item = _safe_get(f"{_HN_FIREBASE_API}/item/{sid}.json").json()
        except (httpx.HTTPError, ValueError):
            continue
        if not item or item.get("type") != "story":
            continue
        score = item.get("score", 0)
        if score < min_score:
            continue
        url = _sanitize_url(item.get("url", ""))
        if url is None:
            url = f"https://news.ycombinator.com/item?id={sid}"
        items.append(
            NewsItem(
                title=item.get("title", "")[:300],
                url=url,
                source="Hacker News",
                summary=f"Score: {score} | Comments: {item.get('descendants', 0)}",
                metadata={
                    "score": score,
                    "comments": item.get("descendants", 0),
                    "source_rank": len(items) + 1,
                },
            )
        )
        if len(items) >= max_items:
            break

    return items


def _fetch_hn_historical(target: date, max_items: int) -> list[NewsItem]:
    """Algolia HN search for stories submitted on `target` (UTC)."""
    start = int(datetime(target.year, target.month, target.day, tzinfo=timezone.utc).timestamp())
    end = start + 86400
    try:
        resp = _safe_get(
            _HN_ALGOLIA_API,
            params={
                "tags": "story",
                "numericFilters": f"created_at_i>={start},created_at_i<{end}",
                "hitsPerPage": str(min(max_items * 4, 50)),
            },
        )
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    hits = data.get("hits", []) if isinstance(data, dict) else []
    # Sort by points so the day's "top" stories come first.
    hits = sorted(hits, key=lambda h: h.get("points") or 0, reverse=True)

    items: list[NewsItem] = []
    for hit in hits:
        title = (hit.get("title") or "")[:300]
        if not title:
            continue
        score = hit.get("points") or 0
        comments = hit.get("num_comments") or 0
        story_url = _sanitize_url(hit.get("url") or "")
        if story_url is None:
            story_url = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        items.append(
            NewsItem(
                title=title,
                url=story_url,
                source="Hacker News",
                summary=f"Score: {score} | Comments: {comments}",
                published_at=hit.get("created_at", ""),
                metadata={
                    "score": score,
                    "comments": comments,
                    "source_rank": len(items) + 1,
                },
            )
        )
        if len(items) >= max_items:
            break
    return items


def fetch_hacker_news(
    max_items: int = 5,
    min_score: int = 50,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Top HN stories. Past dates use Algolia search by submission date."""
    max_items = min(max(1, max_items), 100)
    target = _resolve_date(target_date)

    cached = cache_get("hn", target)
    if cached is not None:
        return cached[:max_items]

    if _is_past(target):
        items = _fetch_hn_historical(target, max_items)
    else:
        items = _fetch_hn_live(max_items, min_score)

    cache_put("hn", target, items)
    return items


# ---------------------------------------------------------------------------
# Reddit finance — live only (no reliable historical free API)
# ---------------------------------------------------------------------------

_REDDIT_SUBREDDITS = ["investing", "stocks", "finance"]


def fetch_reddit_finance(
    max_items: int = 5,
    subreddits: list[str] | None = None,
    sort: str = "top",
    target_date: date | None = None,
) -> list[NewsItem]:
    """Top finance posts. Past dates return cached or empty."""
    max_items = min(max(1, max_items), 100)
    target = _resolve_date(target_date)
    sort = sort if sort in {"top", "hot", "rising"} else "top"

    cached = cache_get("reddit", target)
    if cached is not None:
        return cached[:max_items]
    if _is_past(target):
        return []  # No reliable historical API; don't pollute past dates.

    subs = subreddits or _REDDIT_SUBREDDITS
    items: list[NewsItem] = []
    seen: set[str] = set()

    for sub in subs:
        if not re.match(r"^[A-Za-z0-9_]{1,30}$", sub):
            continue
        try:
            resp = _safe_get(
                f"https://www.reddit.com/r/{sub}/{sort}.json",
                headers={
                    "User-Agent": "EarningsAnalyzer/0.1 (news summary tool)",
                },
                params={
                    "limit": str(min(max_items * 3, 100)),
                    **({"t": "day"} if sort == "top" else {}),
                },
            )
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            continue

        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            title = post.get("title", "")[:300]
            if not title or title in seen:
                continue
            seen.add(title)

            post_url = _sanitize_url(post.get("url", ""))
            if post_url is None:
                post_url = f"https://www.reddit.com{post.get('permalink', '')}"

            items.append(
                NewsItem(
                    title=title,
                    url=post_url,
                    source=f"r/{sub}",
                    summary=(
                        f"Score: {post.get('score', 0)} | "
                        f"Comments: {post.get('num_comments', 0)}"
                    ),
                    published_at=datetime.fromtimestamp(
                        post.get("created_utc", 0), tz=timezone.utc
                    ).isoformat()
                    if post.get("created_utc")
                    else "",
                    metadata={
                        "score": post.get("score", 0),
                        "comments": post.get("num_comments", 0),
                        "upvote_ratio": post.get("upvote_ratio", 0),
                        "source_rank": len(items) + 1,
                    },
                )
            )
            if len(items) >= max_items:
                return items

    return items


# ---------------------------------------------------------------------------
# SEC EDGAR — full-text search supports per-day filing date filters
# ---------------------------------------------------------------------------

_SEC_FULL_TEXT = "https://efts.sec.gov/LATEST/search-index"
_SEC_BROWSE_RSS = "https://www.sec.gov/cgi-bin/browse-edgar"
_SEC_MATERIAL_FORMS = {"8-K", "10-Q", "10-K", "6-K", "S-1", "424B4"}
_SEC_MATERIAL_TERMS = (
    "2.02",
    "7.01",
    "8.01",
    "earnings",
    "results of operations",
    "guidance",
    "material",
    "acquisition",
    "merger",
)


def _is_material_sec_item(form: str, text: str) -> bool:
    """Return True when a filing is likely market-moving."""
    form_upper = form.upper()
    if form_upper in {"10-Q", "10-K", "S-1", "424B4"}:
        return True
    haystack = text.lower()
    return form_upper in _SEC_MATERIAL_FORMS and any(
        term in haystack for term in _SEC_MATERIAL_TERMS
    )


def _sec_news_item_is_material(item: NewsItem) -> bool:
    form = str(item.metadata.get("form", ""))
    if not form:
        title_match = re.match(r"^\s*([A-Za-z0-9\-/]+):", item.title)
        form = title_match.group(1) if title_match else ""
    return bool(item.metadata.get("material")) or _is_material_sec_item(
        form, f"{item.title} {item.summary}"
    )


def _fetch_sec_historical(
    target: date, form_types: list[str], max_items: int
) -> list[NewsItem]:
    iso = target.isoformat()
    forms_param = ",".join(
        f for f in form_types if re.match(r"^[A-Za-z0-9\-/]{1,10}$", f)
    )
    if not forms_param:
        return []
    try:
        # Note: passing q='' returns zero hits — omit q entirely.
        resp = _safe_get(
            _SEC_FULL_TEXT,
            headers={"User-Agent": "EarningsAnalyzer/0.1 admin@example.com"},
            params={
                "dateRange": "custom",
                "startdt": iso,
                "enddt": iso,
                "forms": forms_param,
            },
        )
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    hits = (
        data.get("hits", {}).get("hits", []) if isinstance(data, dict) else []
    )
    items: list[NewsItem] = []
    for hit in hits[:max_items]:
        src = hit.get("_source", {}) if isinstance(hit, dict) else {}
        cik_list = src.get("ciks") or []
        cik = (cik_list[0] if cik_list else "").lstrip("0") or ""
        form = src.get("form", "")
        display_names = src.get("display_names") or []
        company = display_names[0] if display_names else "Unknown"
        title = f"{form}: {company}"[:300]
        if cik:
            url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&CIK={cik}&type={form}"
            )
        else:
            url = "https://www.sec.gov/cgi-bin/browse-edgar"
        items.append(
            NewsItem(
                title=title,
                url=url,
                source="SEC EDGAR",
                summary=src.get("file_date", iso),
                published_at=src.get("file_date", iso),
                metadata={
                    "form": form,
                    "company": company,
                    "material": _is_material_sec_item(form, " ".join(display_names)),
                    "source_rank": len(items) + 1,
                },
            )
        )
    return items


def _fetch_sec_live(form_types: list[str], max_items: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for form in form_types:
        if not re.match(r"^[A-Za-z0-9\-/]{1,10}$", form):
            continue
        try:
            resp = _safe_get(
                _SEC_BROWSE_RSS,
                headers={"User-Agent": "EarningsAnalyzer/0.1 admin@example.com"},
                params={
                    "action": "getcompany",
                    "type": form,
                    "dateb": "",
                    "owner": "exclude",
                    "count": str(min(max_items, 40)),
                    "output": "atom",
                },
            )
        except (httpx.HTTPError, ValueError):
            continue

        try:
            root = ET.fromstring(resp.content[:_MAX_RESPONSE_BYTES])
        except ET.ParseError:
            continue

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            summary_el = entry.find("atom:summary", ns)
            if title_el is None or link_el is None:
                continue
            url = _sanitize_url(link_el.get("href", ""))
            if url is None:
                continue
            items.append(
                NewsItem(
                    title=(title_el.text or "")[:300],
                    url=url,
                    source="SEC EDGAR",
                    summary=(summary_el.text or "")[:200] if summary_el is not None else "",
                    metadata={
                        "form": form,
                        "material": _is_material_sec_item(
                            form,
                            " ".join(
                                (
                                    title_el.text or "",
                                    summary_el.text if summary_el is not None else "",
                                )
                            ),
                        ),
                        "source_rank": len(items) + 1,
                    },
                )
            )
            if len(items) >= max_items:
                return items
    return items


def fetch_sec_filings(
    form_types: list[str] | None = None,
    max_items: int = 5,
    material_only: bool = False,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Latest SEC filings. Past dates use full-text search with date filter."""
    max_items = min(max(1, max_items), 50)
    target = _resolve_date(target_date)
    types = form_types or ["8-K", "10-Q"]

    cached = cache_get("sec", target)
    if cached is not None:
        if material_only:
            cached = [item for item in cached if _sec_news_item_is_material(item)]
        return cached[:max_items]

    if _is_past(target):
        items = _fetch_sec_historical(target, types, max_items)
    else:
        items = _fetch_sec_live(types, max_items)

    if material_only:
        items = [item for item in items if _sec_news_item_is_material(item)]

    cache_put("sec", target, items)
    return items


# ---------------------------------------------------------------------------
# X / Twitter — fetch actual posts (API v2 > Playwright > syndication)
# ---------------------------------------------------------------------------

_DEFAULT_X_ACCOUNTS = [
    # AI labs
    "OpenAI",
    "AnthropicAI",
    "GoogleDeepMind",
    # Researchers & builders
    "karpathy",
    "ylecun",
    "DrJimFan",
    "fchollet",
    "polynoamial",
    # Synthesizers & commentators
    "emollick",
    "swyx",
    "GaryMarcus",
    # Media & podcasters
    "lexfridman",
    "saranormous",
]

_X_API_SEARCH = "https://api.twitter.com/2/tweets/search/recent"
_SYNDICATION_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name"


def _fetch_x_via_api(
    accounts: list[str],
    bearer_token: str,
    max_per_account: int,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Fetch tweets via X API v2 (requires bearer token).

    For ``target_date`` in the past, narrows the search window to that day
    (UTC). Otherwise uses a 48-hour rolling window.
    """
    if target_date and _is_past(target_date):
        start_dt = datetime(
            target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc
        )
        end_dt = start_dt + timedelta(days=1)
        start_time = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        start_time = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time = None

    items: list[NewsItem] = []
    for handle in accounts:
        if not re.match(r"^[A-Za-z0-9_]{1,30}$", handle):
            continue
        params: dict[str, str] = {
            "query": f"from:{handle} -is:retweet",
            "max_results": str(min(max_per_account, 10)),
            "tweet.fields": "text,created_at,author_id",
            "start_time": start_time,
        }
        if end_time:
            params["end_time"] = end_time
        try:
            resp = _safe_get(
                _X_API_SEARCH,
                headers={"Authorization": f"Bearer {bearer_token}"},
                params=params,
            )
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            continue

        for tweet in data.get("data", [])[:max_per_account]:
            tweet_id = tweet.get("id", "")
            text = tweet.get("text", "")[:300]
            if not text:
                continue
            items.append(
                NewsItem(
                    title=text,
                    url=f"https://x.com/{handle}/status/{tweet_id}",
                    source=f"@{handle}",
                    summary=tweet.get("created_at", ""),
                    published_at=tweet.get("created_at", ""),
                    metadata={"handle": handle, "source_rank": len(items) + 1},
                )
            )
    return items


def _fetch_x_via_browser(
    accounts: list[str],
    max_per_account: int,
) -> list[NewsItem]:
    """Fetch tweets using a headless browser (Playwright)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    items: list[NewsItem] = []
    seen_urls: set[str] = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            )

            for handle in accounts:
                if not re.match(r"^[A-Za-z0-9_]{1,30}$", handle):
                    continue
                try:
                    page.goto(
                        f"https://x.com/{handle}",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    page.wait_for_selector(
                        'article[data-testid="tweet"]',
                        timeout=15_000,
                    )
                except Exception:
                    continue

                tweets = page.query_selector_all('article[data-testid="tweet"]')
                count = 0
                for tweet_el in tweets:
                    if count >= max_per_account:
                        break

                    # Skip pinned tweets — they have a "Pinned" label
                    social_context = tweet_el.query_selector(
                        '[data-testid="socialContext"]'
                    )
                    if social_context:
                        label = social_context.inner_text().strip().lower()
                        if "pinned" in label:
                            continue

                    text_el = tweet_el.query_selector('[data-testid="tweetText"]')
                    if not text_el:
                        continue
                    text = text_el.inner_text()[:300].strip()
                    if not text:
                        continue

                    time_link = tweet_el.query_selector('a[href*="/status/"]')
                    if time_link:
                        href = time_link.get_attribute("href") or ""
                        url = f"https://x.com{href}" if href.startswith("/") else href
                    else:
                        url = f"https://x.com/{handle}"

                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    items.append(
                        NewsItem(
                            title=text,
                            url=url,
                            source=f"@{handle}",
                            summary="",
                            metadata={"handle": handle, "source_rank": len(items) + 1},
                        )
                    )
                    count += 1

            browser.close()
    except Exception:
        pass

    return items


def _fetch_x_via_syndication(
    accounts: list[str],
    max_per_account: int,
) -> list[NewsItem]:
    """Last-resort fallback via X syndication embed endpoint."""
    items: list[NewsItem] = []
    for handle in accounts:
        if not re.match(r"^[A-Za-z0-9_]{1,30}$", handle):
            continue
        try:
            resp = _safe_get(
                f"{_SYNDICATION_URL}/{handle}",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://platform.twitter.com/",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
        except (httpx.HTTPError, ValueError):
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        count = 0
        for tweet_div in soup.select("[data-tweet-id]"):
            text_el = tweet_div.select_one(".timeline-Tweet-text")
            if text_el is None:
                continue
            text = text_el.get_text(strip=True)[:300]
            if not text:
                continue
            tweet_id = tweet_div.get("data-tweet-id", "")
            items.append(
                NewsItem(
                    title=text,
                    url=f"https://x.com/{handle}/status/{tweet_id}",
                    source=f"@{handle}",
                    summary="",
                    metadata={"handle": handle, "source_rank": len(items) + 1},
                )
            )
            count += 1
            if count >= max_per_account:
                break

    return items


def fetch_x_posts(
    accounts: list[str] | None = None,
    bearer_token: str | None = None,
    max_per_account: int = 3,
    max_total: int = 15,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Recent posts from X. Past dates require API bearer or return cached/empty."""
    target = _resolve_date(target_date)
    cached = cache_get("x", target)
    if cached is not None:
        return cached[:max_total]
    if _is_past(target) and not bearer_token:
        return []  # No reliable scrape-based historical path.

    accts = accounts or _DEFAULT_X_ACCOUNTS
    if bearer_token:
        items = _fetch_x_via_api(accts, bearer_token, max_per_account, target_date=target)[:max_total]
    else:
        items = _fetch_x_via_browser(accts, max_per_account)[:max_total]
        if not items:
            items = _fetch_x_via_syndication(accts, max_per_account)[:max_total]

    cache_put("x", target, items)
    return items


# ---------------------------------------------------------------------------
# ArXiv — submittedDate range supports per-day filtering
# ---------------------------------------------------------------------------

_ARXIV_API = "http://export.arxiv.org/api/query"
_ARXIV_CATEGORIES = "cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CL"


def fetch_arxiv_papers(
    max_items: int = 5,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Latest AI/ML papers from ArXiv. Past dates filter by submittedDate."""
    max_items = min(max(1, max_items), 50)
    target = _resolve_date(target_date)

    cached = cache_get("arxiv", target)
    if cached is not None:
        return cached[:max_items]

    if _is_past(target):
        ymd = target.strftime("%Y%m%d")
        date_filter = f"+AND+submittedDate:[{ymd}0000+TO+{ymd}2359]"
        url = (
            f"{_ARXIV_API}?search_query=({_ARXIV_CATEGORIES}){date_filter}"
            f"&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={max_items}"
        )
    else:
        url = (
            f"{_ARXIV_API}?search_query={_ARXIV_CATEGORIES}"
            f"&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={max_items}"
        )

    try:
        resp = _safe_get(url, timeout=15)
    except (httpx.HTTPError, ValueError):
        return []

    try:
        root = ET.fromstring(resp.content[:_MAX_RESPONSE_BYTES])
    except ET.ParseError:
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    items: list[NewsItem] = []
    for entry in root.findall("a:entry", ns)[:max_items]:
        title = (entry.findtext("a:title", "", ns) or "").strip()
        title = re.sub(r"\s+", " ", title)[:300]
        link = entry.findtext("a:id", "", ns) or ""
        summary = (entry.findtext("a:summary", "", ns) or "").strip()
        summary = re.sub(r"\s+", " ", summary)[:200]
        published = entry.findtext("a:published", "", ns) or entry.findtext("a:updated", "", ns) or ""
        if not title or not link:
            continue
        items.append(
            NewsItem(
                title=title,
                url=link,
                source="ArXiv",
                summary=summary,
                published_at=published,
                metadata={"source_rank": len(items) + 1},
            )
        )

    cache_put("arxiv", target, items)
    return items


# ---------------------------------------------------------------------------
# Hugging Face Daily Papers — supports ?date=YYYY-MM-DD
# ---------------------------------------------------------------------------

_HF_PAPERS_API = "https://huggingface.co/api/daily_papers"


def fetch_hf_papers(
    max_items: int = 5,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Trending HF Daily Papers. Past dates use the date query param."""
    max_items = min(max(1, max_items), 50)
    target = _resolve_date(target_date)

    cached = cache_get("hf", target)
    if cached is not None:
        return cached[:max_items]

    params = {"date": target.isoformat()} if _is_past(target) else None
    try:
        resp = _safe_get(_HF_PAPERS_API, params=params, timeout=15)
        papers = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    if not isinstance(papers, list):
        return []

    items: list[NewsItem] = []
    for entry in papers[:max_items]:
        paper = entry.get("paper", {})
        title = (paper.get("title") or "")[:300]
        paper_id = paper.get("id", "")
        summary = (paper.get("summary") or "")[:200]
        upvotes = paper.get("upvotes", 0)
        if not title or not paper_id:
            continue
        url = f"https://huggingface.co/papers/{paper_id}"
        items.append(
            NewsItem(
                title=title,
                url=url,
                source="HF Papers",
                summary=f"Upvotes: {upvotes} | {summary}" if summary else "",
                metadata={"upvotes": upvotes, "source_rank": len(items) + 1},
            )
        )

    cache_put("hf", target, items)
    return items


# ---------------------------------------------------------------------------
# Financial Times — RSS only exposes recent items, no historical archive
# ---------------------------------------------------------------------------

_DEFAULT_FT_FEEDS: list[dict[str, str]] = [
    {"title": "Technology", "url": "https://www.ft.com/technology?format=rss"},
    {"title": "Artificial Intelligence", "url": "https://www.ft.com/artificial-intelligence?format=rss"},
]


def fetch_ft_articles(
    sections: list[dict[str, str]] | None = None,
    max_items: int = 5,
    target_date: date | None = None,
) -> list[NewsItem]:
    """FT article headlines via public RSS. Past dates return cached or empty."""
    target = _resolve_date(target_date)

    cached = cache_get("ft", target)
    if cached is not None:
        return cached[:max_items]
    if _is_past(target):
        return []

    ft_feeds = sections or _DEFAULT_FT_FEEDS
    items: list[NewsItem] = []
    seen: set[str] = set()

    for feed_info in ft_feeds:
        feed_url = feed_info.get("url", "")
        if "format=rss" not in feed_url:
            feed_url = feed_url.rstrip("/") + "?format=rss"
        safe_url = _sanitize_url(feed_url)
        if not safe_url:
            continue

        try:
            resp = _safe_get(safe_url)
        except (httpx.HTTPError, ValueError):
            continue

        try:
            root = ET.fromstring(resp.content[:_MAX_RESPONSE_BYTES])
        except ET.ParseError:
            continue

        count = 0
        for item_el in root.findall(".//item"):
            title = (item_el.findtext("title") or "").strip()[:300]
            link = (item_el.findtext("link") or "").strip()
            desc = (item_el.findtext("description") or "").strip()[:200]
            published = _parse_feed_datetime(
                item_el.findtext("pubDate") or item_el.findtext("published")
            )
            if not title or not link or title in seen:
                continue
            article_url = _sanitize_url(link)
            if not article_url:
                continue
            seen.add(title)

            items.append(
                NewsItem(
                    title=title,
                    url=article_url,
                    source=f"FT {feed_info.get('title', '')}",
                    summary=desc,
                    published_at=published,
                    metadata={"source_rank": len(items) + 1},
                )
            )
            count += 1
            if count >= max_items:
                break
        if len(items) >= max_items:
            break

    return items[:max_items]

# ---------------------------------------------------------------------------
# Official updates - first-party RSS/Atom feeds for labs, vendors, and policy
# ---------------------------------------------------------------------------

_DEFAULT_OFFICIAL_FEEDS: list[dict[str, str]] = [
    {"title": "OpenAI News", "url": "https://openai.com/news/rss.xml"},
    {"title": "Google AI", "url": "https://blog.google/technology/ai/rss/"},
    {"title": "Microsoft AI", "url": "https://blogs.microsoft.com/ai/feed/"},
]


def fetch_official_updates(
    feeds: list[dict[str, str]] | None = None,
    max_items: int = 6,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Fetch first-party RSS/Atom updates. Past dates return cached or empty."""
    max_items = min(max(1, max_items), 50)
    target = _resolve_date(target_date)

    cached = cache_get("official", target)
    if cached is not None:
        return cached[:max_items]
    if _is_past(target):
        return []

    feed_list = feeds or _DEFAULT_OFFICIAL_FEEDS
    items: list[NewsItem] = []
    seen: set[str] = set()

    for feed in feed_list:
        feed_url = _sanitize_url(feed.get("url", ""))
        if not feed_url:
            continue
        try:
            resp = _safe_get(feed_url)
        except (httpx.HTTPError, ValueError):
            continue

        try:
            root = ET.fromstring(resp.content[:_MAX_RESPONSE_BYTES])
        except ET.ParseError:
            continue

        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//item"):
            title = (entry.findtext("title") or "").strip()[:300]
            link = (entry.findtext("link") or "").strip()
            summary = (entry.findtext("description") or "").strip()[:250]
            published = _parse_feed_datetime(entry.findtext("pubDate"))
            _append_official_item(items, seen, feed, title, link, summary, published)
            if len(items) >= max_items:
                cache_put("official", target, items)
                return items

        for entry in root.findall(".//a:entry", ns):
            title = (entry.findtext("a:title", "", ns) or "").strip()[:300]
            link_el = entry.find("a:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            summary = (
                entry.findtext("a:summary", "", ns)
                or entry.findtext("a:content", "", ns)
                or ""
            ).strip()[:250]
            published = (
                entry.findtext("a:published", "", ns)
                or entry.findtext("a:updated", "", ns)
                or ""
            )
            _append_official_item(items, seen, feed, title, link, summary, published)
            if len(items) >= max_items:
                cache_put("official", target, items)
                return items

    cache_put("official", target, items)
    return items


def _append_official_item(
    items: list[NewsItem],
    seen: set[str],
    feed: dict[str, str],
    title: str,
    link: str,
    summary: str,
    published: str,
) -> None:
    url = _sanitize_url(link)
    if not title or not url:
        return
    key = url.lower()
    if key in seen:
        return
    seen.add(key)
    items.append(
        NewsItem(
            title=title,
            url=url,
            source=f"Official {feed.get('title', '').strip()}".strip(),
            summary=BeautifulSoup(summary, "lxml").get_text(" ", strip=True)[:200],
            published_at=published,
            metadata={"source_rank": len(items) + 1},
        )
    )


# ---------------------------------------------------------------------------
# Spotify - episode publish dates available, filter to that day on past runs
# ---------------------------------------------------------------------------

_DEFAULT_SPOTIFY_SHOWS: list[dict[str, str]] = [
    {"title": "The All-In Podcast", "url": "https://open.spotify.com/show/2IqXAVFR4e0Bmyjsdc8QzF"},
    {"title": "Acquired", "url": "https://open.spotify.com/show/7Fj0XEuUQLbqnTICXBSKAE"},
    {"title": "Lex Fridman Podcast", "url": "https://open.spotify.com/show/2MAi0BvDc6GTFvKFPXnkCL"},
    {"title": "Odd Lots (Bloomberg)", "url": "https://open.spotify.com/show/35IczmCnU09IEcz5P5jZ89"},
]

_SPOTIFY_OEMBED = "https://open.spotify.com/oembed"


def _episode_published_on(target: date, ld_date: str | None) -> bool:
    """Return True if the JSON-LD datePublished string matches `target`."""
    if not ld_date:
        return False
    return ld_date[:10] == target.isoformat()


def fetch_spotify_episodes(
    shows: list[dict[str, str]] | None = None,
    max_episodes: int = 1,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Latest episode per podcast. Past dates filter to that day's publishes."""
    target = _resolve_date(target_date)

    cached = cache_get("spotify", target)
    if cached is not None:
        return cached

    podcast_list = shows or _DEFAULT_SPOTIFY_SHOWS
    items: list[NewsItem] = []
    past = _is_past(target)

    for show in podcast_list:
        show_url = _sanitize_url(show.get("url", ""))
        if not show_url:
            continue
        show_title = show.get("title", "Podcast")

        try:
            resp = _safe_get(show_url)
        except (httpx.HTTPError, ValueError):
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        import json as _json

        # Tuple: (url, title, desc, published)
        episode_links: list[tuple[str, str, str, str]] = []

        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld = _json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            episodes = []
            if isinstance(ld, dict):
                episodes = ld.get("episode", [])
                if isinstance(episodes, dict):
                    episodes = [episodes]
            for ep in episodes:
                ep_url = ep.get("url", "")
                ep_title = ep.get("name", "")[:300]
                ep_desc = ep.get("description", "")[:500]
                ep_pub = ep.get("datePublished", "") or ep.get("uploadDate", "")
                if ep_url and ep_title:
                    episode_links.append((ep_url, ep_title, ep_desc, ep_pub))

        if past:
            episode_links = [
                e for e in episode_links if _episode_published_on(target, e[3])
            ]

        episode_links = episode_links[:max_episodes]

        if not episode_links and not past:
            try:
                oembed_resp = _safe_get(_SPOTIFY_OEMBED, params={"url": show_url})
                oembed = oembed_resp.json()
                items.append(
                    NewsItem(
                        title=f"{show_title}: Latest Episode",
                        url=show_url,
                        source="Spotify",
                        summary=oembed.get("title", "")[:200],
                        metadata={"show": show_title, "source_rank": len(items) + 1},
                    )
                )
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            continue

        for ep_url, ep_title, ep_desc, _pub in episode_links:
            if not ep_desc:
                try:
                    ep_resp = _safe_get(ep_url)
                    ep_soup = BeautifulSoup(ep_resp.text, "lxml")
                    meta_desc = ep_soup.select_one('meta[name="description"]')
                    if meta_desc:
                        ep_desc = meta_desc.get("content", "")[:500]
                    if not ep_desc:
                        for script in ep_soup.select(
                            'script[type="application/ld+json"]'
                        ):
                            try:
                                ld = _json.loads(script.string or "")
                            except (ValueError, TypeError):
                                continue
                            if isinstance(ld, dict) and ld.get("description"):
                                ep_desc = ld["description"][:500]
                                break
                except (httpx.HTTPError, ValueError):
                    pass

            items.append(
                NewsItem(
                    title=f"{show_title}: {ep_title}",
                    url=ep_url,
                    source="Spotify",
                    summary=ep_desc[:200] if ep_desc else "",
                    published_at=_pub,
                    metadata={"show": show_title, "source_rank": len(items) + 1},
                )
            )

    cache_put("spotify", target, items)
    return items


# ---------------------------------------------------------------------------
# Viral tweet extraction (cross-source + oEmbed)
# ---------------------------------------------------------------------------

_TWEET_URL_RE = re.compile(
    r"https?://(?:x\.com|twitter\.com)/(\w+)/status/(\d+)",
)

_OEMBED_URL = "https://publish.twitter.com/oembed"


def _extract_tweet_urls(sources: list[list[NewsItem]]) -> list[str]:
    """Scan all collected news items for tweet/X URLs."""
    urls: list[str] = []
    seen_ids: set[str] = set()

    for source_list in sources:
        for item in source_list:
            for text in (item.url, item.summary):
                for m in _TWEET_URL_RE.finditer(text):
                    tweet_id = m.group(2)
                    if tweet_id not in seen_ids:
                        seen_ids.add(tweet_id)
                        urls.append(
                            f"https://x.com/{m.group(1)}/status/{tweet_id}"
                        )
    return urls


def _find_sharing_context(
    tweet_url: str,
    sources: list[list[NewsItem]],
) -> str:
    """Find which non-X source shared this tweet (the commentary layer)."""
    tweet_id = _TWEET_URL_RE.search(tweet_url)
    if not tweet_id:
        return ""
    tid = tweet_id.group(2)

    contexts: list[str] = []
    for source_list in sources:
        for item in source_list:
            if item.source.startswith("@") or item.source == "X":
                continue
            if tid in item.url or tid in item.summary:
                contexts.append(f"Shared on {item.source}: {item.title[:100]}")
    return " | ".join(contexts[:3])


def fetch_viral_tweets(
    sources: list[list[NewsItem]],
    max_items: int = 10,
    target_date: date | None = None,
) -> list[NewsItem]:
    """Extract tweet URLs from other sources, enrich via oEmbed.

    Tweets that got shared on Techmeme, HN, or Reddit are inherently
    higher-signal — someone outside X found them worth discussing.
    Cached per-day; viral tweets for a given day's source set are
    reproducible from those sources.
    """
    max_items = min(max(1, max_items), 50)
    target = _resolve_date(target_date)

    cached = cache_get("viral_tweets", target)
    if cached is not None:
        return cached[:max_items]

    tweet_urls = _extract_tweet_urls(sources)
    if not tweet_urls:
        return []

    items: list[NewsItem] = []
    for tweet_url in tweet_urls[:max_items * 2]:
        try:
            resp = _safe_get(
                _OEMBED_URL,
                params={"url": tweet_url, "omit_script": "true"},
                timeout=8,
            )
            data = resp.json()
        except (httpx.HTTPError, ValueError, KeyError):
            continue

        author = data.get("author_name", "Unknown")
        raw_html = data.get("html", "")
        if raw_html:
            text = BeautifulSoup(raw_html, "lxml").get_text(strip=True)[:300]
        else:
            text = ""

        context = _find_sharing_context(tweet_url, sources)
        summary_parts = [text]
        if context:
            summary_parts.append(context)

        items.append(
            NewsItem(
                title=f"@{author}",
                url=tweet_url,
                source="X (via cross-post)",
                summary=" | ".join(summary_parts)[:500],
                metadata={"cross_post_context": context, "source_rank": len(items) + 1},
            )
        )
        if len(items) >= max_items:
            break

    cache_put("viral_tweets", target, items)
    return items


# ---------------------------------------------------------------------------
# Aggregate all sources
# ---------------------------------------------------------------------------


def gather_daily_news(
    techmeme_count: int = 5,
    hn_count: int = 5,
    reddit_count: int = 5,
    sec_count: int = 5,
    arxiv_count: int = 5,
    hf_count: int = 5,
    x_accounts: list[str] | None = None,
    x_bearer_token: str | None = None,
    ft_sections: list[dict[str, str]] | None = None,
    official_feeds: list[dict[str, str]] | None = None,
    spotify_podcasts: list[dict[str, str]] | None = None,
    reddit_subreddits: list[str] | None = None,
    reddit_sort: str = "top",
    sec_form_types: list[str] | None = None,
    sec_material_only: bool = False,
    active_topics: list[str] | None = None,
    topic_profiles: dict[str, list[str]] | None = None,
    source_weights: dict[str, float] | None = None,
    target_date: date | None = None,
) -> DailyNewsSources:
    """Collect news from all configured sources for the given date.

    Defaults to today. Past dates take historical paths where supported and
    are served from disk cache on subsequent runs.
    """
    target = _resolve_date(target_date)

    techmeme = fetch_techmeme_headlines(max_items=techmeme_count, target_date=target)
    hn = fetch_hacker_news(max_items=hn_count, target_date=target)
    reddit = fetch_reddit_finance(
        max_items=reddit_count,
        subreddits=reddit_subreddits,
        sort=reddit_sort,
        target_date=target,
    )
    sec = fetch_sec_filings(
        form_types=sec_form_types,
        max_items=sec_count,
        material_only=sec_material_only,
        target_date=target,
    )

    viral = fetch_viral_tweets(
        sources=[techmeme, hn, reddit, sec], target_date=target
    )

    news = DailyNewsSources(
        date=target,
        techmeme_headlines=techmeme,
        hacker_news=hn,
        reddit_finance=reddit,
        sec_filings=sec,
        viral_tweets=viral,
        x_links=fetch_x_posts(
            accounts=x_accounts, bearer_token=x_bearer_token, target_date=target
        ),
        ft_links=fetch_ft_articles(sections=ft_sections, target_date=target),
        official_updates=fetch_official_updates(
            feeds=official_feeds, target_date=target
        ),
        spotify_links=fetch_spotify_episodes(
            shows=spotify_podcasts, target_date=target
        ),
        arxiv_papers=fetch_arxiv_papers(max_items=arxiv_count, target_date=target),
        hf_papers=fetch_hf_papers(max_items=hf_count, target_date=target),
    )
    news.ranked_items = rank_daily_news(
        news,
        active_topics=active_topics,
        topic_profiles=topic_profiles,
        source_weights=source_weights,
    )
    return news
