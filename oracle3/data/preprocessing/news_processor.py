"""News preprocessing pipeline with relevance filtering, deduplication, and optional LLM summarization."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stopwords (small hardcoded English set)
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset(
    {
        'a',
        'an',
        'the',
        'and',
        'or',
        'but',
        'in',
        'on',
        'at',
        'to',
        'for',
        'of',
        'with',
        'by',
        'from',
        'is',
        'it',
        'that',
        'this',
        'was',
        'are',
        'be',
        'has',
        'had',
        'have',
        'will',
        'would',
        'could',
        'should',
        'not',
        'what',
        'which',
        'who',
        'when',
        'where',
        'how',
        'if',
        'than',
        'so',
        'no',
        'do',
        'does',
        'did',
        'as',
        'its',
        'my',
        'he',
        'she',
        'they',
    }
)


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alpha characters, remove stopwords."""
    words = re.split(r'[^a-zA-Z]+', text.lower())
    return {w for w in words if w and w not in _STOPWORDS}


# ---------------------------------------------------------------------------
# NewsArticle
# ---------------------------------------------------------------------------


@dataclass
class NewsArticle:
    """Normalized news article from any source."""

    title: str
    snippet: str
    source: str  # "google_news", "twitter", "reddit", "government", etc.
    url: str
    published_at: datetime
    raw_text: str  # full text if available, else snippet
    relevance_score: float = 0.0  # 0-1, set by relevance filter
    credibility_score: float = 0.5  # 0-1, based on source
    is_duplicate: bool = False
    cluster_id: str = ''  # group ID for deduplication clusters
    summary: str = ''  # LLM-generated summary (if summarized)
    sentiment: str = ''  # "positive", "negative", "neutral"
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# NewsRelevanceFilter
# ---------------------------------------------------------------------------


class NewsRelevanceFilter:
    """Scores and filters news articles by relevance to a market question."""

    SOURCE_CREDIBILITY: ClassVar[dict[str, float]] = {
        'reuters': 0.95,
        'ap': 0.95,
        'bbc': 0.90,
        'wsj': 0.90,
        'bloomberg': 0.90,
        'cnbc': 0.85,
        'nytimes': 0.85,
        'washingtonpost': 0.85,
        'foxnews': 0.75,
        'cnn': 0.80,
        'twitter': 0.60,
        'reddit': 0.40,
        'congress.gov': 0.98,
        'whitehouse.gov': 0.95,
        'federalregister.gov': 0.95,
        'default': 0.50,
    }

    def __init__(self, min_relevance: float = 0.2) -> None:
        self.min_relevance = min_relevance

    # -- helpers ----------------------------------------------------------

    def _get_credibility(self, source: str) -> float:
        """Look up credibility for *source*, falling back to 'default'."""
        key = source.lower().strip()
        if key in self.SOURCE_CREDIBILITY:
            return self.SOURCE_CREDIBILITY[key]
        # Try matching as substring (e.g. source="Reuters News" -> "reuters")
        for known, score in self.SOURCE_CREDIBILITY.items():
            if known in key or key in known:
                return score
        return self.SOURCE_CREDIBILITY['default']

    @staticmethod
    def _freshness_score(
        published_at: datetime, half_life_hours: float = 24.0
    ) -> float:
        """Exponential decay based on article age. Half-life defaults to 24 h."""
        now = datetime.now(timezone.utc)
        pub = (
            published_at
            if published_at.tzinfo
            else published_at.replace(tzinfo=timezone.utc)
        )
        age_hours = max((now - pub).total_seconds() / 3600.0, 0.0)
        return math.exp(-math.log(2) * age_hours / half_life_hours)

    # -- public API -------------------------------------------------------

    def score_relevance(
        self,
        article: NewsArticle,
        market_question: str,
        keywords: list[str] | None = None,
    ) -> float:
        """Score article relevance to a market question.

        Scoring components (weighted sum):
          1. Keyword overlap  (40%): fraction of market-question keywords found in article
          2. Title match      (30%): Jaccard similarity between question words and title words
          3. Source credibility(15%): from SOURCE_CREDIBILITY map
          4. Freshness        (15%): exponential decay based on age (half-life = 24 hours)

        Returns a float in [0, 1].
        """
        question_tokens = _tokenize(market_question)
        if keywords:
            question_tokens |= {k.lower() for k in keywords}

        article_tokens = _tokenize(article.raw_text or article.snippet)
        title_tokens = _tokenize(article.title)

        # 1. Keyword overlap
        if question_tokens:
            keyword_overlap = len(question_tokens & article_tokens) / len(
                question_tokens
            )
        else:
            keyword_overlap = 0.0

        # 2. Title match (Jaccard)
        union = question_tokens | title_tokens
        if union:
            title_jaccard = len(question_tokens & title_tokens) / len(union)
        else:
            title_jaccard = 0.0

        # 3. Source credibility (normalised to 0-1 already)
        credibility = self._get_credibility(article.source)

        # 4. Freshness
        freshness = self._freshness_score(article.published_at)

        score = (
            0.40 * keyword_overlap
            + 0.30 * title_jaccard
            + 0.15 * credibility
            + 0.15 * freshness
        )
        return min(max(score, 0.0), 1.0)

    def filter_articles(
        self,
        articles: list[NewsArticle],
        market_question: str,
        keywords: list[str] | None = None,
        top_k: int = 10,
    ) -> list[NewsArticle]:
        """Score, filter, and rank articles by relevance.

        1. Score each article.
        2. Set ``article.relevance_score``.
        3. Set ``article.credibility_score`` from SOURCE_CREDIBILITY.
        4. Filter out articles below ``min_relevance``.
        5. Sort by ``relevance_score`` descending.
        6. Return top *top_k*.
        """
        for article in articles:
            article.relevance_score = self.score_relevance(
                article, market_question, keywords
            )
            article.credibility_score = self._get_credibility(article.source)

        relevant = [a for a in articles if a.relevance_score >= self.min_relevance]
        relevant.sort(key=lambda a: a.relevance_score, reverse=True)
        logger.debug(
            'Relevance filter: %d/%d articles passed (min_relevance=%.2f)',
            len(relevant),
            len(articles),
            self.min_relevance,
        )
        return relevant[:top_k]


# ---------------------------------------------------------------------------
# NewsDeduplicator
# ---------------------------------------------------------------------------


class NewsDeduplicator:
    """Deduplicates news articles using title similarity and SimHash."""

    def __init__(self, similarity_threshold: float = 0.6) -> None:
        self.similarity_threshold = similarity_threshold

    # -- fingerprinting ---------------------------------------------------

    @staticmethod
    def _simhash(text: str, hash_bits: int = 64) -> int:
        """Compute a SimHash fingerprint for *text*.

        1. Tokenize into words.
        2. Hash each word with ``hashlib.md5``, take first 8 bytes as int.
        3. For each bit position: if bit is 1, add weight (+1); else subtract (-1).
        4. Final hash: bit is 1 if sum > 0, else 0.
        """
        tokens = re.split(r'[^a-zA-Z0-9]+', text.lower())
        tokens = [t for t in tokens if t]

        if not tokens:
            return 0

        v = [0] * hash_bits
        for token in tokens:
            h = int.from_bytes(
                hashlib.md5(token.encode('utf-8')).digest()[:8],  # noqa: S324
                byteorder='big',
            )
            for i in range(hash_bits):
                if h & (1 << i):
                    v[i] += 1
                else:
                    v[i] -= 1

        fingerprint = 0
        for i in range(hash_bits):
            if v[i] > 0:
                fingerprint |= 1 << i
        return fingerprint

    @staticmethod
    def _hamming_distance(a: int, b: int) -> int:
        """Count differing bits between two integers."""
        return bin(a ^ b).count('1')

    def _jaccard_similarity(self, text_a: str, text_b: str) -> float:
        """Jaccard similarity between tokenized texts."""
        tokens_a = _tokenize(text_a)
        tokens_b = _tokenize(text_b)
        if not tokens_a and not tokens_b:
            return 1.0
        if not tokens_a or not tokens_b:
            return 0.0
        return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)

    # -- public API -------------------------------------------------------

    def deduplicate(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """Remove duplicate articles, keeping the highest-credibility version.

        Algorithm:
          1. Compute a fingerprint for each article (simhash of title + first 100 words of snippet).
          2. Group articles with similar fingerprints (hamming distance < threshold based on
             ``similarity_threshold``) OR high Jaccard title similarity.
          3. Within each group, keep the article with highest ``credibility_score``.
          4. Mark others as ``is_duplicate = True``, set ``cluster_id``.
          5. Return only non-duplicate articles, sorted by ``published_at`` descending.
        """
        if not articles:
            return []

        # Hamming distance threshold: lower similarity_threshold -> stricter matching
        # With 64-bit hashes, two identical texts have distance 0.
        # We allow up to (1 - similarity_threshold) * 64 differing bits.
        max_hamming = int((1 - self.similarity_threshold) * 64)

        # Compute fingerprints
        fingerprints: list[int] = []
        for article in articles:
            first_100 = ' '.join((article.snippet or '').split()[:100])
            text = f'{article.title} {first_100}'
            fingerprints.append(self._simhash(text))

        # Union-Find for clustering
        parent: list[int] = list(range(len(articles)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        # Pairwise comparison
        for i in range(len(articles)):
            for j in range(i + 1, len(articles)):
                if (
                    self._hamming_distance(fingerprints[i], fingerprints[j])
                    <= max_hamming
                ):
                    union(i, j)
                elif (
                    self._jaccard_similarity(articles[i].title, articles[j].title)
                    >= self.similarity_threshold
                ):
                    union(i, j)

        # Build clusters
        clusters: dict[int, list[int]] = {}
        for i in range(len(articles)):
            root = find(i)
            clusters.setdefault(root, []).append(i)

        # Within each cluster, keep the best article
        for indices in clusters.values():
            cluster_id = uuid.uuid4().hex[:12]
            # Assign cluster_id to all members
            for idx in indices:
                articles[idx].cluster_id = cluster_id

            if len(indices) == 1:
                continue

            # Sort by credibility descending, then by published_at descending (prefer newer)
            indices.sort(
                key=lambda idx: (
                    articles[idx].credibility_score,
                    articles[idx].published_at,
                ),
                reverse=True,
            )
            # First one is the "keeper"; mark the rest as duplicates
            for idx in indices[1:]:
                articles[idx].is_duplicate = True

        kept = [a for a in articles if not a.is_duplicate]
        kept.sort(key=lambda a: a.published_at, reverse=True)
        logger.debug(
            'Deduplication: kept %d/%d articles (%d duplicates removed)',
            len(kept),
            len(articles),
            len(articles) - len(kept),
        )
        return kept


# ---------------------------------------------------------------------------
# NewsSummarizer
# ---------------------------------------------------------------------------


class NewsSummarizer:
    """Summarizes batches of news articles using a cheap LLM."""

    def __init__(
        self,
        model: str = 'claude-haiku-4-5-20251001',
        max_tokens: int = 256,
        temperature: float = 0.1,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    async def summarize_article(self, article: NewsArticle) -> str:
        """Summarize a single article into 1-2 sentences using LLM."""
        import litellm  # noqa: F811

        content = (article.raw_text or article.snippet or article.title)[:2000]
        prompt = (
            'Summarize the following news article in 1-2 concise sentences. '
            'Focus on the key facts and any market implications.\n\n'
            f'Title: {article.title}\n'
            f'Source: {article.source}\n'
            f'Content: {content}'
        )
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
            return response.choices[0].message.content.strip()
        except Exception:
            logger.warning(
                "LLM summarization failed for '%s', using title as fallback",
                article.title,
                exc_info=True,
            )
            return article.title

    async def summarize_batch(
        self, articles: list[NewsArticle], market_question: str
    ) -> str:
        """Summarize multiple articles into a combined brief for a market.

        Produces 3-5 bullet points of key developments and their likely impact
        on market probability.
        """
        import litellm  # noqa: F811

        article_texts = []
        for i, a in enumerate(articles, 1):
            snippet = (a.raw_text or a.snippet or a.title)[:500]
            article_texts.append(f'{i}. [{a.source}] {a.title}\n   {snippet}')

        joined = '\n\n'.join(article_texts)
        prompt = (
            f'Given these {len(articles)} articles about "{market_question}", '
            'produce a 3-5 bullet point summary of the key developments and '
            'their likely impact on the market probability.\n\n'
            f'{joined}'
        )
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=self.max_tokens * 2,
                temperature=self.temperature,
            )
            return response.choices[0].message.content.strip()
        except Exception:
            logger.warning('LLM batch summarization failed', exc_info=True)
            return '\n'.join(f'- {a.title}' for a in articles)

    async def batch_summarize_articles(
        self, articles: list[NewsArticle]
    ) -> list[NewsArticle]:
        """Summarize each article in batch (up to 5 concurrently).

        Uses ``asyncio.Semaphore(5)`` to limit concurrency.
        Sets ``article.summary`` for each article.
        Returns articles with summaries populated.
        """
        sem = asyncio.Semaphore(5)

        async def _summarize(article: NewsArticle) -> None:
            async with sem:
                article.summary = await self.summarize_article(article)

        await asyncio.gather(*[_summarize(a) for a in articles])
        return articles


# ---------------------------------------------------------------------------
# NewsProcessor — unified pipeline
# ---------------------------------------------------------------------------


class NewsProcessor:
    """Complete news preprocessing pipeline: filter -> deduplicate -> summarize."""

    def __init__(
        self,
        min_relevance: float = 0.2,
        similarity_threshold: float = 0.6,
        summarize: bool = False,
        summary_model: str = 'claude-haiku-4-5-20251001',
        top_k: int = 10,
    ) -> None:
        self.filter = NewsRelevanceFilter(min_relevance=min_relevance)
        self.dedup = NewsDeduplicator(similarity_threshold=similarity_threshold)
        self.summarizer = NewsSummarizer(model=summary_model) if summarize else None
        self.top_k = top_k

    async def process(
        self,
        articles: list[NewsArticle],
        market_question: str,
        keywords: list[str] | None = None,
    ) -> list[NewsArticle]:
        """Run the full preprocessing pipeline.

        1. Score relevance and filter.
        2. Deduplicate.
        3. Optionally summarize (if ``self.summarizer`` is set).
        4. Return processed articles.
        """
        logger.info('NewsProcessor: starting pipeline with %d articles', len(articles))

        # 1. Relevance filter
        filtered = self.filter.filter_articles(
            articles, market_question, keywords, top_k=self.top_k
        )
        logger.info(
            'NewsProcessor: %d articles after relevance filtering', len(filtered)
        )

        # 2. Deduplicate
        deduped = self.dedup.deduplicate(filtered)
        logger.info('NewsProcessor: %d articles after deduplication', len(deduped))

        # 3. Summarize (optional)
        if self.summarizer and deduped:
            deduped = await self.summarizer.batch_summarize_articles(deduped)
            logger.info('NewsProcessor: summarization complete')

        return deduped

    def format_for_prompt(
        self, articles: list[NewsArticle], max_articles: int = 5
    ) -> str:
        """Format processed articles into a string for LLM prompt injection.

        For each article (up to *max_articles*)::

            [{source}] {title} ({age}) [relevance: {score:.0%}]
            {summary or snippet}
        """
        if not articles:
            return '(No relevant news articles found.)'

        now = datetime.now(timezone.utc)
        lines: list[str] = []

        for article in articles[:max_articles]:
            pub = article.published_at
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            age_hours = (now - pub).total_seconds() / 3600.0

            if age_hours < 1:
                age_str = f'{int(age_hours * 60)}m ago'
            elif age_hours < 24:
                age_str = f'{int(age_hours)}h ago'
            else:
                age_str = f'{int(age_hours / 24)}d ago'

            body = article.summary or article.snippet or article.raw_text
            # Truncate body to keep prompt compact
            if len(body) > 200:
                body = body[:200] + '...'

            lines.append(
                f'[{article.source}] {article.title} ({age_str}) '
                f'[relevance: {article.relevance_score:.0%}]\n'
                f'  {body}'
            )

        return '\n\n'.join(lines)

    @staticmethod
    def from_news_events(events: list) -> list[NewsArticle]:
        """Convert :class:`NewsEvent` objects to :class:`NewsArticle` objects.

        Accepts ``list[NewsEvent]`` (imported from ``swm_agent.events.events``).
        """
        articles: list[NewsArticle] = []
        for ev in events:
            articles.append(
                NewsArticle(
                    title=ev.title or '',
                    snippet=ev.description or ev.news or '',
                    source=ev.source or '',
                    url=ev.url or '',
                    published_at=ev.published_at or datetime.now(timezone.utc),
                    raw_text=ev.news or ev.description or '',
                    metadata={
                        'categories': getattr(ev, 'categories', []),
                        'uuid': getattr(ev, 'uuid', ''),
                        'event_id': getattr(ev, 'event_id', ''),
                        'image_url': getattr(ev, 'image_url', ''),
                    },
                )
            )
        return articles

    @staticmethod
    def from_raw_dicts(dicts: list[dict]) -> list[NewsArticle]:
        """Convert raw article dicts (from fetchers) to :class:`NewsArticle` objects.

        Expected dict keys: ``title``, ``snippet``, ``source``, ``link`` or ``url``,
        ``date`` or ``published_at``.
        """
        articles: list[NewsArticle] = []
        for d in dicts:
            # Parse published date
            raw_date = d.get('published_at') or d.get('date')
            if isinstance(raw_date, datetime):
                pub = raw_date
            elif isinstance(raw_date, str):
                try:
                    pub = datetime.fromisoformat(raw_date)
                except ValueError:
                    logger.warning("Could not parse date '%s', using now()", raw_date)
                    pub = datetime.now(timezone.utc)
            else:
                pub = datetime.now(timezone.utc)

            snippet = d.get('snippet', '') or d.get('description', '') or ''
            articles.append(
                NewsArticle(
                    title=d.get('title', ''),
                    snippet=snippet,
                    source=d.get('source', ''),
                    url=d.get('url', '') or d.get('link', ''),
                    published_at=pub,
                    raw_text=d.get('raw_text', '') or d.get('content', '') or snippet,
                    metadata={
                        k: v
                        for k, v in d.items()
                        if k
                        not in {
                            'title',
                            'snippet',
                            'description',
                            'source',
                            'url',
                            'link',
                            'date',
                            'published_at',
                            'raw_text',
                            'content',
                        }
                    },
                )
            )
        return articles
