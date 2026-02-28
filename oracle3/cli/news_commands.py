"""CLI commands for standalone news fetching."""

from __future__ import annotations

import asyncio
import json
import uuid

import click
import feedparser
import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GOOGLE_NEWS_RSS_BASE = 'https://news.google.com/rss'

WSJ_RSS_FEEDS = {
    'https://feeds.content.dowjones.io/public/rss/RSSWorldNews': ['world'],
    'https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness': ['business'],
    'https://feeds.content.dowjones.io/public/rss/RSSMarketsMain': ['finance'],
    'https://feeds.content.dowjones.io/public/rss/RSSWSJD': ['technology'],
    'https://feeds.content.dowjones.io/public/rss/RSSUSnews': ['us'],
    'https://feeds.content.dowjones.io/public/rss/socialpoliticsfeed': ['politics'],
}


def _format_article(article: dict) -> str:
    lines = []
    title = article.get('title', '(no title)')
    source = article.get('source', '')
    url = article.get('url', '')
    published = article.get('published_at', '')
    description = article.get('description', '')

    lines.append(f'  Title:     {title}')
    if source:
        lines.append(f'  Source:    {source}')
    if published:
        lines.append(f'  Published: {published}')
    if description:
        snippet = description[:120] + ('…' if len(description) > 120 else '')
        lines.append(f'  Snippet:   {snippet}')
    if url:
        lines.append(f'  URL:       {url}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Async fetchers
# ---------------------------------------------------------------------------


async def _fetch_google_news(query: str | None, limit: int) -> list[dict]:
    """Fetch from Google News RSS — general or search feed."""
    feedparser.CACHE_DIRECTORY = None
    feedparser._check_cache = lambda *a, **kw: None  # noqa: SLF001

    if query:
        import urllib.parse

        q_encoded = urllib.parse.quote(query)
        feed_url = (
            f'{GOOGLE_NEWS_RSS_BASE}/search?q={q_encoded}&hl=en-US&gl=US&ceid=US:en'
        )
    else:
        feed_url = f'{GOOGLE_NEWS_RSS_BASE}?hl=en-US&gl=US&ceid=US:en'

    feed = await asyncio.to_thread(feedparser.parse, feed_url)
    articles: list[dict] = []

    for entry in (feed.entries or [])[:limit]:
        title = entry.get('title', '')
        link = entry.get('link', '')
        source = ''
        if hasattr(entry, 'source') and hasattr(entry.source, 'title'):
            source = entry.source.title

        published_at = ''
        if 'published' in entry:
            published_at = entry.published

        articles.append(
            {
                'uuid': entry.get('id', link) or str(uuid.uuid4()),
                'title': title,
                'source': source or 'Google News',
                'url': link,
                'published_at': published_at,
                'description': entry.get('summary', ''),
            }
        )
    return articles


async def _fetch_rss(query: str | None, limit: int) -> list[dict]:
    """Fetch from WSJ RSS feeds, optionally filtering by query string."""
    feedparser.CACHE_DIRECTORY = None
    feedparser._check_cache = lambda *a, **kw: None  # noqa: SLF001

    articles: list[dict] = []
    for feed_url, _tags in WSJ_RSS_FEEDS.items():
        if len(articles) >= limit:
            break
        try:
            feed = await asyncio.to_thread(feedparser.parse, feed_url)
            feed_title = getattr(getattr(feed, 'feed', None), 'title', feed_url)
            for entry in feed.entries or []:
                if len(articles) >= limit:
                    break
                title = entry.get('title', '')
                description = entry.get('summary', entry.get('description', ''))
                if query:
                    q_lower = query.lower()
                    if (
                        q_lower not in title.lower()
                        and q_lower not in description.lower()
                    ):
                        continue
                link = entry.get('link', '')
                guid = entry.get('id', link) or str(uuid.uuid4())
                pub = ''
                if 'published' in entry:
                    pub = entry.published
                articles.append(
                    {
                        'uuid': guid,
                        'title': title,
                        'source': feed_title,
                        'url': link,
                        'published_at': pub,
                        'description': description,
                    }
                )
        except Exception:
            continue
    return articles


async def _fetch_thenewsapi(
    query: str | None, limit: int, api_token: str
) -> list[dict]:
    """Fetch from TheNewsAPI."""
    params: dict = {
        'api_token': api_token,
        'language': 'en',
        'limit': min(limit, 25),
    }
    if query:
        params['search'] = query
        url = 'https://api.thenewsapi.com/v1/news/all'
    else:
        url = 'https://api.thenewsapi.com/v1/news/headlines'

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, params=params)

    if response.status_code != 200:
        raise click.ClickException(
            f'TheNewsAPI returned HTTP {response.status_code}: {response.text[:200]}'
        )
    data = response.json()
    raw_articles = data.get('data', [])
    articles = []
    for a in raw_articles[:limit]:
        articles.append(
            {
                'uuid': a.get('uuid', ''),
                'title': a.get('title', ''),
                'source': a.get('source', ''),
                'url': a.get('url', ''),
                'published_at': a.get('published_at', ''),
                'description': a.get('description', ''),
            }
        )
    return articles


# ---------------------------------------------------------------------------
# Click group + commands
# ---------------------------------------------------------------------------


@click.group()
def news() -> None:
    """Standalone news fetching commands."""


@news.command('fetch')
@click.option(
    '--source',
    type=click.Choice(['google', 'rss', 'thenewsapi']),
    default='google',
    show_default=True,
    help='News source to fetch from.',
)
@click.option('--query', default=None, help='Optional search/filter query.')
@click.option(
    '--limit', default=10, show_default=True, type=int, help='Max articles to fetch.'
)
@click.option(
    '--api-token',
    default=None,
    help='TheNewsAPI token (or THENEWSAPI_TOKEN env var). Required for --source thenewsapi.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Output as JSON.')
def news_fetch(
    source: str, query: str | None, limit: int, api_token: str | None, as_json: bool
) -> None:
    """Fetch news headlines from a specified source."""
    import os

    token = api_token or os.environ.get('THENEWSAPI_TOKEN', '')

    try:
        if source == 'google':
            articles = asyncio.run(_fetch_google_news(query, limit))
        elif source == 'rss':
            articles = asyncio.run(_fetch_rss(query, limit))
        else:
            if not token:
                raise click.ClickException(
                    'TheNewsAPI requires a token. Pass --api-token or set THENEWSAPI_TOKEN.'
                )
            articles = asyncio.run(_fetch_thenewsapi(query, limit, token))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch news: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps({'source': source, 'count': len(articles), 'articles': articles})
        )
        return

    if not articles:
        click.echo('No articles found.')
        return

    click.echo(f'Fetched {len(articles)} article(s) from {source}:\n')
    for i, article in enumerate(articles, 1):
        click.echo(f'[{i}]')
        click.echo(_format_article(article))
        click.echo()
