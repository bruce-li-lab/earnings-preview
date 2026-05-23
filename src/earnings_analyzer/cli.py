"""Command-line interface for the earnings analyzer."""

from __future__ import annotations

import re

import click
from rich.console import Console

from earnings_analyzer.analyzer import analyze_earnings

console = Console()

_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,10}$")


@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(package_name="earnings-analyzer")
def main(ctx: click.Context) -> None:
    """Earnings Analyzer - earnings reports & daily news summaries."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.argument("ticker")
def earnings(ticker: str) -> None:
    """Analyze quarterly earnings for TICKER (e.g., AAPL, MSFT)."""
    if not _TICKER_RE.match(ticker):
        console.print("[red]Invalid ticker format.[/red]")
        raise click.Abort()
    try:
        analyze_earnings(ticker)
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
    except Exception as e:
        console.print(f"[red]Error analyzing {ticker.upper()}: {e}[/red]")
        raise click.Abort()


@main.command("news")
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(exists=False),
    help="Path to news_config.json (optional).",
)
@click.option(
    "--output-dir",
    default=None,
    help="Directory to save the newsletter HTML (overrides config).",
)
@click.option(
    "--headlines",
    default=None,
    type=click.IntRange(1, 100),
    help="Number of Techmeme headlines (1-100).",
)
@click.option(
    "--analyze/--no-analyze",
    "run_analysis",
    default=True,
    help="Run AI analysis on collected headlines (requires GEMINI_API_KEY).",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=False,
    help="Open the newsletter in a browser after generation.",
)
@click.option(
    "--obsidian-vault",
    default=None,
    type=click.Path(exists=False),
    help="Path to Obsidian vault to export daily note (overrides config).",
)
@click.option(
    "--date",
    "override_date",
    default=None,
    help="Override the note date (YYYY-MM-DD). Content is still live news.",
)
def daily_news(
    config_path: str | None,
    output_dir: str | None,
    headlines: int | None,
    run_analysis: bool,
    open_browser: bool,
    obsidian_vault: str | None,
    override_date: str | None,
) -> None:
    """Generate today's daily news summary as an HTML newsletter.

    Pulls headlines from Techmeme, Hacker News, Reddit, and SEC EDGAR,
    then runs AI analysis to separate signal from noise.  Includes
    curated links to X, Financial Times, and Spotify podcasts.

    Customize sources by creating a news_config.json file or
    passing --config.
    """
    from earnings_analyzer.news_analyzer import analyze_news
    from earnings_analyzer.news_config import NewsConfig
    from earnings_analyzer.news_sources import gather_daily_news
    from earnings_analyzer.newsletter import save_newsletter
    from earnings_analyzer.obsidian import export_to_obsidian, save_markdown

    try:
        cfg = NewsConfig.load(config_path)

        if output_dir is not None:
            if ".." in output_dir:
                console.print("[red]--output-dir must not contain '..'[/red]")
                raise click.Abort()
            cfg.output_dir = output_dir
        if headlines is not None:
            cfg.techmeme_count = headlines
        if obsidian_vault is not None:
            cfg.obsidian_vault = obsidian_vault

        # Parse optional date override
        note_date = None
        if override_date:
            from datetime import date as _date
            try:
                note_date = _date.fromisoformat(override_date)
            except ValueError:
                console.print(f"[red]Invalid date format: {override_date} (use YYYY-MM-DD)[/red]")
                raise click.Abort()

        console.print("[bold cyan]Gathering daily news...[/bold cyan]")
        news = gather_daily_news(
            techmeme_count=cfg.techmeme_count,
            hn_count=cfg.hn_count,
            reddit_count=cfg.reddit_count,
            sec_count=cfg.sec_count,
            arxiv_count=cfg.arxiv_count,
            hf_count=cfg.hf_count,
            x_accounts=cfg.x_accounts,
            x_bearer_token=cfg.x_bearer_token,
            ft_sections=cfg.ft_sections,
            official_feeds=cfg.official_feeds,
            spotify_podcasts=cfg.spotify_podcasts,
            reddit_subreddits=cfg.reddit_subreddits,
            reddit_sort=cfg.reddit_sort,
            sec_form_types=cfg.sec_form_types,
            sec_material_only=cfg.sec_material_only,
            active_topics=cfg.active_topics,
            topic_profiles=cfg.topic_profiles,
            source_weights=cfg.source_weights,
            target_date=note_date,
        )

        total = (
            len(news.techmeme_headlines)
            + len(news.hacker_news)
            + len(news.reddit_finance)
            + len(news.sec_filings)
            + len(news.arxiv_papers)
            + len(news.hf_papers)
            + len(news.official_updates)
        )
        console.print(f"  [dim]Collected {total} items from live sources[/dim]")

        if run_analysis:
            console.print("[bold cyan]Running AI analysis...[/bold cyan]")
            news.analysis = analyze_news(news)
            if "unavailable" in news.analysis.lower():
                console.print(
                    "  [yellow]AI analysis unavailable — "
                    "set GEMINI_API_KEY for full brief[/yellow]"
                )
            else:
                console.print("  [dim]Analysis complete[/dim]")

        filepath = save_newsletter(news, output_dir=cfg.output_dir)
        md_path = save_markdown(news, output_dir=cfg.output_dir)

        console.print(
            f"\n[green]Newsletter saved:[/green] {filepath}\n"
            f"  [green]Markdown saved:[/green]  {md_path}\n"
            f"  [dim]{len(news.techmeme_headlines)} Techmeme, "
            f"{len(news.hacker_news)} HN, "
            f"{len(news.arxiv_papers)} ArXiv, "
            f"{len(news.hf_papers)} HF, "
            f"{len(news.reddit_finance)} Reddit, "
            f"{len(news.sec_filings)} SEC, "
            f"{len(news.x_links)} X, "
            f"{len(news.ft_links)} FT, "
            f"{len(news.official_updates)} official, "
            f"{len(news.spotify_links)} podcasts[/dim]"
        )

        if cfg.obsidian_vault:
            console.print("[bold cyan]Exporting to Obsidian vault...[/bold cyan]")
            vault_path = export_to_obsidian(news, vault_path=cfg.obsidian_vault)
            console.print(f"  [green]Obsidian note saved:[/green] {vault_path}")

        if open_browser:
            import webbrowser

            webbrowser.open(filepath.resolve().as_uri())

    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
    except Exception as e:
        console.print(f"[red]Error generating news summary: {e}[/red]")
        raise click.Abort()


if __name__ == "__main__":
    main()
