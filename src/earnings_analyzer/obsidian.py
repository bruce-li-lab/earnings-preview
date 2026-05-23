"""Export daily news to Obsidian-compatible markdown notes."""

from __future__ import annotations

import re
from pathlib import Path

from earnings_analyzer.news_sources_types import DailyNewsSources, NewsItem
from earnings_analyzer.url_security import sanitize_url

_ITEM_RE = re.compile(
    r"^- \[(?P<title>[^\]]+)\]\((?P<url>[^\)]+)\)"
    r"(?:\s+`(?P<source>[^`]*)`)?",
)


def _clean_md_text(text: str) -> str:
    """Keep source text on one markdown-safe line."""
    cleaned = re.sub(r"[\r\n\t]+", " ", text)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    return cleaned.replace("[", "\\[").replace("]", "\\]").replace("`", "'").strip()


def _clean_md_url(url: str) -> str:
    safe = sanitize_url(url)
    if not safe:
        return "https://example.invalid"
    return safe.replace(")", "%29").replace("(", "%28")


def _render_items(items: list[NewsItem], header: str, icon: str) -> str:
    """Render a list of news items as a markdown section."""
    if not items:
        return ""
    lines = [f"## {icon} {header}\n"]
    for item in items:
        line = f"- [{_clean_md_text(item.title)}]({_clean_md_url(item.url)})"
        if item.source:
            line += f" `{_clean_md_text(item.source)}`"
        lines.append(line)
        if item.rank_reason:
            lines.append(f"  - Rank: {_clean_md_text(item.rank_reason)}")
        if item.summary:
            lines.append(f"  - {_clean_md_text(item.summary)}")
    lines.append("")
    return "\n".join(lines)


def _render_analysis(analysis: str) -> str:
    """Render the AI analysis section."""
    if not analysis:
        return ""
    return f"## AI Intelligence Brief\n\n{analysis}\n\n"


def _parse_existing_items(text: str) -> dict[str, list[NewsItem]]:
    """Parse an existing note and extract items grouped by section header."""
    sections: dict[str, list[NewsItem]] = {}
    current_section: str | None = None
    current_items: list[NewsItem] = []
    last_item: NewsItem | None = None

    for line in text.splitlines():
        if line.startswith("## ") and "AI Intelligence Brief" not in line:
            if current_section is not None:
                sections[current_section] = current_items
            header_text = line[3:].strip()
            parts = header_text.split(" ", 1)
            current_section = parts[1] if len(parts) > 1 else parts[0]
            current_items = []
            last_item = None
            continue

        if current_section is None:
            continue

        m = _ITEM_RE.match(line)
        if m:
            last_item = NewsItem(
                title=m.group("title").replace("\\[", "[").replace("\\]", "]"),
                url=m.group("url"),
                source=m.group("source") or "",
            )
            current_items.append(last_item)
        elif line.startswith("  - ") and last_item is not None:
            detail = line[4:]
            if detail.startswith("Rank: "):
                last_item.rank_reason = detail[6:]
            else:
                last_item.summary = detail

    if current_section is not None:
        sections[current_section] = current_items

    return sections


def _merge_items(existing: list[NewsItem], new: list[NewsItem]) -> list[NewsItem]:
    """Merge two item lists, deduplicating by URL. Existing items come first."""
    seen_urls: set[str] = set()
    merged: list[NewsItem] = []
    for item in existing:
        if item.url not in seen_urls:
            seen_urls.add(item.url)
            merged.append(item)
    for item in new:
        if item.url not in seen_urls:
            seen_urls.add(item.url)
            merged.append(item)
    return merged


_SECTION_MAP: list[tuple[str, str, str]] = [
    ("Ranked Top Stories", "*", "ranked_items"),
    ("Techmeme Headlines", "-", "techmeme_headlines"),
    ("Hacker News", "-", "hacker_news"),
    ("X / Twitter", "-", "x_links"),
    ("Financial Times", "-", "ft_links"),
    ("Official Updates", "-", "official_updates"),
    ("Podcast Episodes", "-", "spotify_links"),
    ("ArXiv Papers", "-", "arxiv_papers"),
    ("HF Daily Papers", "-", "hf_papers"),
    ("Reddit Finance", "-", "reddit_finance"),
    ("SEC Filings", "-", "sec_filings"),
]

_TAG_TERMS = [
    "openai",
    "anthropic",
    "google",
    "meta",
    "microsoft",
    "gpt",
    "claude",
    "gemini",
    "llama",
    "llm",
    "transformer",
    "diffusion",
    "agent",
    "agi",
]


def _collect_tags(all_items: list[NewsItem]) -> list[str]:
    """Build tag list from item titles."""
    tags = ["daily-news", "ai"]
    source_tags: set[str] = set()
    for item in all_items:
        title_lower = item.title.lower()
        for term in _TAG_TERMS:
            if term in title_lower:
                source_tags.add(term)
    tags.extend(sorted(source_tags))
    return tags


def render_obsidian_note(
    news: DailyNewsSources,
    existing_sections: dict[str, list[NewsItem]] | None = None,
) -> str:
    """Generate a full Obsidian markdown note, merging with existing items."""
    date_str = news.date.isoformat()
    formatted = news.date.strftime("%A, %B %d, %Y").replace(" 0", " ")

    section_blocks: list[str] = []
    all_items: list[NewsItem] = []
    total = 0

    section_blocks.append(_render_analysis(news.analysis))

    for header, icon, field in _SECTION_MAP:
        new_items: list[NewsItem] = getattr(news, field, [])
        old_items = (existing_sections or {}).get(header, [])
        merged = _merge_items(old_items, new_items)
        total += len(merged)
        all_items.extend(merged)
        section_blocks.append(_render_items(merged, header, icon))

    tags = _collect_tags(all_items)
    tags_yaml = ", ".join(tags)

    frontmatter = f"""---
date: {date_str}
type: daily-news
tags: [{tags_yaml}]
sources: {total}
---
"""

    header = f"# Daily AI & Tech News - {formatted}\n\n"
    from datetime import date as _date

    prev = _date.fromordinal(news.date.toordinal() - 1)
    nav = f"[[{date_str} |Today]] | [[{prev.isoformat()}|Previous]]\n\n"
    body = "".join(s for s in section_blocks if s)

    return frontmatter + header + nav + body


def export_to_obsidian(
    news: DailyNewsSources,
    vault_path: str | Path,
) -> Path:
    """Export the daily news to a markdown note in the Obsidian vault."""
    vault = Path(vault_path).resolve()
    daily_dir = vault
    daily_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{news.date.isoformat()}.md"
    filepath = (daily_dir / filename).resolve()

    try:
        filepath.relative_to(vault)
    except ValueError:
        raise ValueError("Filename escapes vault directory")

    existing_sections: dict[str, list[NewsItem]] | None = None
    if filepath.exists():
        existing_text = filepath.read_text(encoding="utf-8")
        existing_sections = _parse_existing_items(existing_text)

    filepath.write_text(
        render_obsidian_note(news, existing_sections=existing_sections),
        encoding="utf-8",
    )
    return filepath


def save_markdown(
    news: DailyNewsSources,
    output_dir: str | Path = ".",
    filename: str | None = None,
) -> Path:
    """Save a clean Obsidian-ready markdown file alongside the HTML newsletter."""
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    if filename is None:
        filename = f"news-summary-{news.date.isoformat()}.md"

    if ".." in filename or "/" in filename or "\\" in filename:
        raise ValueError(f"Invalid filename: {filename}")

    filepath = (output_path / filename).resolve()
    try:
        filepath.relative_to(output_path)
    except ValueError:
        raise ValueError("Filename escapes output directory")

    filepath.write_text(render_obsidian_note(news), encoding="utf-8")
    return filepath
