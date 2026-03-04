#!/usr/bin/env python3
"""Crawl overlapping market data from Polymarket and Kalshi for arb backtesting.

Steps:
1. Fetch active markets from both platforms (public APIs, no auth needed).
2. Match markets using the coinjure multi-stage pipeline (or fallback to legacy).
3. For each matched pair, fetch price history from both platforms.
4. Write an interleaved JSONL file suitable for cross-platform backtest.

Usage:
    python scripts/crawl_cross_platform_data.py [--output data/cross_platform/matched.jsonl]
    python scripts/crawl_cross_platform_data.py --use-pipeline  # use coinjure pipeline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_API = 'https://gamma-api.polymarket.com'
CLOB_API = 'https://clob.polymarket.com'
KALSHI_API = 'https://api.elections.kalshi.com/trade-api/v2'

MIN_SIMILARITY = 0.40
_STOPWORDS = frozenset(
    {
        'will',
        'the',
        'a',
        'an',
        'of',
        'in',
        'on',
        'by',
        'to',
        'for',
        'be',
        'is',
        'at',
        'before',
        'after',
    }
)


def _normalize(text: str) -> str:
    text = re.sub(r'[^a-z0-9\s]', ' ', text.lower())
    tokens = [t for t in text.split() if t not in _STOPWORDS]
    return ' '.join(tokens)


def _extract_keyphrases(text_norm: str) -> set[str]:
    """Extract single keywords (4+ chars) and bigrams (3+ chars each)."""
    words = text_norm.split()
    phrases: set[str] = set()
    for w in words:
        if len(w) >= 4:
            phrases.add(w)
    for i in range(len(words) - 1):
        if len(words[i]) >= 3 and len(words[i + 1]) >= 3:
            phrases.add(f'{words[i]} {words[i + 1]}')
    return phrases


# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------


def fetch_polymarket_markets(max_events: int = 400) -> list[dict]:
    """Fetch active Polymarket events with their markets."""
    print(f'[Polymarket] Fetching up to {max_events} active events...')
    all_markets: list[dict] = []
    offset = 0
    page_size = 100
    n_events = 0

    while offset < max_events:
        try:
            resp = requests.get(
                f'{GAMMA_API}/events',
                params={
                    'active': 'true',
                    'closed': 'false',
                    'limit': min(page_size, max_events - offset),
                    'offset': offset,
                },
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()
            if not events:
                break
            n_events += len(events)
            for ev in events:
                event_id = str(ev.get('id', ''))
                event_title = ev.get('title', '')
                for mkt in ev.get('markets', []):
                    question = mkt.get('question', event_title)
                    raw_clob = mkt.get('clobTokenIds', '[]')
                    if isinstance(raw_clob, str):
                        try:
                            token_ids = json.loads(raw_clob)
                        except (json.JSONDecodeError, TypeError):
                            token_ids = []
                    else:
                        token_ids = raw_clob or []
                    all_markets.append(
                        {
                            'platform': 'polymarket',
                            'event_id': event_id,
                            'market_id': str(mkt.get('id', '')),
                            'question': question,
                            'norm': _normalize(question),
                            'keyphrases': _extract_keyphrases(_normalize(question)),
                            'token_id': token_ids[0] if token_ids else '',
                            'no_token_id': token_ids[1] if len(token_ids) > 1 else '',
                            'volume': float(mkt.get('volume', 0) or 0),
                        }
                    )
            offset += len(events)
            if len(events) < page_size:
                break
            time.sleep(0.3)
        except Exception as exc:
            print(f'  [WARN] page offset={offset}: {exc}')
            break

    print(f'  Found {len(all_markets)} markets from {n_events} events')
    return all_markets


def fetch_kalshi_markets() -> list[dict]:
    """Fetch active Kalshi events+markets via public API."""
    print('[Kalshi] Fetching active events with markets...')
    all_markets: list[dict] = []
    cursor: str | None = None

    for _page in range(15):
        try:
            params: dict = {
                'status': 'open',
                'limit': 100,
                'with_nested_markets': 'true',
            }
            if cursor:
                params['cursor'] = cursor
            resp = requests.get(
                f'{KALSHI_API}/events',
                params=params,
                timeout=20,
                headers={'Accept': 'application/json'},
            )
            resp.raise_for_status()
            data = resp.json()
            events = data.get('events', [])
            if not events:
                break
            for ev in events:
                ev_title = ev.get('title', '')
                for mkt in ev.get('markets', []):
                    title = mkt.get('title', '') or ev_title
                    all_markets.append(
                        {
                            'platform': 'kalshi',
                            'event_ticker': mkt.get(
                                'event_ticker', ev.get('event_ticker', '')
                            ),
                            'market_ticker': mkt.get('ticker', ''),
                            'title': title,
                            'norm': _normalize(title),
                            'keyphrases': _extract_keyphrases(_normalize(title)),
                            'yes_bid': mkt.get('yes_bid'),
                            'yes_ask': mkt.get('yes_ask'),
                            'volume': mkt.get('volume', 0),
                        }
                    )
            cursor = data.get('cursor')
            if not cursor:
                break
            time.sleep(0.3)
        except Exception as exc:
            print(f'  [WARN] Kalshi events page: {exc}')
            break

    print(f'  Found {len(all_markets)} Kalshi markets')
    return all_markets


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match_markets(
    poly_markets: list[dict],
    kalshi_markets: list[dict],
    min_sim: float = MIN_SIMILARITY,
) -> list[dict]:
    """Match Polymarket to Kalshi by keyphrase overlap + SequenceMatcher."""
    print(
        f'\n[Matching] {len(poly_markets)} Poly x {len(kalshi_markets)} Kalshi markets...'
    )

    matches: list[dict] = []
    seen_kalshi: set[str] = set()

    # Sort Poly by volume (high volume first)
    poly_sorted = sorted(poly_markets, key=lambda m: m.get('volume', 0), reverse=True)[
        :300
    ]
    print(f'  Using top {len(poly_sorted)} Polymarket markets by volume')

    for pm in poly_sorted:
        pn = pm['norm']
        p_phrases = pm['keyphrases']

        best_score = 0.0
        best_km: dict | None = None

        for km in kalshi_markets:
            kid = km['market_ticker']
            if kid in seen_kalshi:
                continue
            # Quick filter: at least 2 shared keyphrases
            overlap = p_phrases & km['keyphrases']
            if len(overlap) < 2:
                continue
            score = SequenceMatcher(None, pn, km['norm']).ratio()
            if score > best_score:
                best_score = score
                best_km = km

        if best_km is not None and best_score >= min_sim:
            seen_kalshi.add(best_km['market_ticker'])
            match_id = f'match_{len(matches) + 1:03d}'
            matches.append(
                {
                    'match_id': match_id,
                    'similarity': round(best_score, 3),
                    'poly': pm,
                    'kalshi': best_km,
                    'label': pm['question'][:80],
                }
            )

    matches.sort(key=lambda m: m['similarity'], reverse=True)
    print(f'  Found {len(matches)} matched pairs (min_sim={min_sim})')
    for m in matches[:15]:
        print(
            f'    [{m["similarity"]:.2f}] {m["poly"]["question"][:55]}'
            f'\n           <-> {m["kalshi"]["title"][:55]}'
        )
    if len(matches) > 15:
        print(f'    ... and {len(matches) - 15} more')

    return matches


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------


def fetch_poly_price_history(token_id: str, interval: str = 'max') -> list[dict]:
    """Fetch Polymarket price history for a CLOB token."""
    if not token_id:
        return []
    fidelity_map = {'max': 1, '1d': 1440, '6h': 360, '1h': 60}
    fidelity = fidelity_map.get(interval, 1)
    try:
        resp = requests.get(
            f'{CLOB_API}/prices-history',
            params={'market': token_id, 'interval': interval, 'fidelity': fidelity},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        history = data.get('history', [])
        result = []
        for pt in history:
            t = pt.get('t')
            p = pt.get('p')
            if t is not None and p is not None:
                result.append({'t': int(t), 'p': float(p)})
        return result
    except Exception as exc:
        print(f'    [WARN] Poly history {token_id[:20]}: {exc}')
        return []


def fetch_kalshi_price_history(market_ticker: str) -> list[dict]:
    """Fetch Kalshi price history via candlesticks or orderbook snapshot."""
    if not market_ticker:
        return []

    # Try candlesticks endpoint (various period intervals)
    now_ts = int(time.time())
    for days_back, period in [(30, 60), (90, 1440)]:
        start_ts = now_ts - days_back * 86400
        try:
            resp = requests.get(
                f'{KALSHI_API}/markets/{market_ticker}/candlesticks',
                params={
                    'start_ts': start_ts,
                    'end_ts': now_ts,
                    'period_interval': period,
                },
                timeout=15,
                headers={'Accept': 'application/json'},
            )
            if resp.status_code == 200:
                data = resp.json()
                candles = data.get('candlesticks', [])
                result = []
                for c in candles:
                    t = c.get('end_period_ts') or c.get('start_period_ts')
                    p = c.get('yes_price') or c.get('close') or c.get('price')
                    if t is not None and p is not None:
                        price = float(p) / 100.0 if float(p) > 1.0 else float(p)
                        result.append({'t': int(t), 'p': price})
                if result:
                    return sorted(result, key=lambda x: x['t'])
        except Exception:
            pass

    # Fallback: orderbook snapshot as single point
    try:
        resp = requests.get(
            f'{KALSHI_API}/markets/{market_ticker}/orderbook',
            timeout=10,
            headers={'Accept': 'application/json'},
        )
        if resp.status_code == 200:
            data = resp.json()
            ob = data.get('orderbook', {})
            yes_bids = ob.get('yes', [])
            if yes_bids:
                best_bid = max(yes_bids, key=lambda x: x[0]) if yes_bids else None
                if best_bid:
                    price = (
                        float(best_bid[0]) / 100.0
                        if float(best_bid[0]) > 1.0
                        else float(best_bid[0])
                    )
                    return [{'t': int(time.time()), 'p': price}]
    except Exception:
        pass

    return []


def fetch_histories_for_matches(
    matches: list[dict],
    max_matches: int = 50,
    interval: str = '1h',
) -> list[dict]:
    """Fetch price histories for matched market pairs."""
    results: list[dict] = []
    n = min(len(matches), max_matches)
    print(f'\n[History] Fetching price data for top {n} matched pairs...')

    for i, match in enumerate(matches[:n]):
        label = match['label'][:50]
        print(f'  [{i + 1}/{n}] {label} (sim={match["similarity"]:.2f})')

        # Polymarket
        token_id = match['poly'].get('token_id', '')
        poly_hist = fetch_poly_price_history(token_id, interval=interval)
        print(f'    Polymarket: {len(poly_hist)} points')

        # Kalshi
        market_ticker = match['kalshi'].get('market_ticker', '')
        kalshi_hist = fetch_kalshi_price_history(market_ticker)
        print(f'    Kalshi:     {len(kalshi_hist)} points')

        if not poly_hist and not kalshi_hist:
            print('    Skipping (no data on either side)')
            continue

        match_id = match['match_id']
        pm = match['poly']
        km = match['kalshi']

        # Polymarket row
        results.append(
            {
                'platform': 'polymarket',
                'match_id': match_id,
                'event_id': pm['event_id'],
                'market_id': pm['market_id'],
                'question': pm['question'],
                'ticker': {
                    'symbol': token_id,
                    'name': pm['question'],
                    'token_id': token_id,
                    'no_token_id': pm.get('no_token_id', ''),
                    'market_id': pm['market_id'],
                    'event_id': pm['event_id'],
                },
                'time_series': {'Yes': poly_hist},
            }
        )

        # Kalshi row
        results.append(
            {
                'platform': 'kalshi',
                'match_id': match_id,
                'event_id': km.get('event_ticker', ''),
                'market_id': market_ticker,
                'question': km['title'],
                'ticker': {
                    'symbol': market_ticker,
                    'name': km['title'],
                    'market_ticker': market_ticker,
                    'event_ticker': km.get('event_ticker', ''),
                },
                'time_series': {'Yes': kalshi_hist},
            }
        )

        time.sleep(0.3)  # Rate limiting

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run_pipeline_scan() -> list[dict]:
    """Run the coinjure matching pipeline and convert results to legacy format."""
    from coinjure.matching import MarketMatchingPipeline

    pipeline = MarketMatchingPipeline()
    results = asyncio.run(pipeline.scan())

    matches: list[dict] = []
    for i, r in enumerate(results):
        matches.append({
            'match_id': f'match_{i + 1:03d}',
            'similarity': r.score,
            'poly': {
                'platform': 'polymarket',
                'event_id': r.poly_market.event_id,
                'market_id': r.poly_market.market_id,
                'question': r.poly_market.title,
                'norm': '',
                'keyphrases': set(),
                'token_id': r.poly_market.extra.get('token_id', ''),
                'no_token_id': r.poly_market.extra.get('no_token_id', ''),
                'volume': r.poly_market.extra.get('volume', 0),
            },
            'kalshi': {
                'platform': 'kalshi',
                'event_ticker': r.kalshi_market.event_id,
                'market_ticker': r.kalshi_market.market_id,
                'title': r.kalshi_market.title,
                'norm': '',
                'keyphrases': set(),
                'volume': r.kalshi_market.extra.get('volume', 0),
            },
            'label': r.label,
            'confidence': r.confidence.value,
        })

    print(f'  Pipeline found {len(matches)} matched pairs')
    for m in matches[:15]:
        conf = m.get('confidence', '?')
        print(
            f'    [{m["similarity"]:.2f}|{conf}] '
            f'{m["poly"]["question"][:55]}'
            f'\n           <-> {m["kalshi"]["title"][:55]}'
        )
    if len(matches) > 15:
        print(f'    ... and {len(matches) - 15} more')

    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description='Crawl cross-platform market data')
    parser.add_argument(
        '--output',
        default='data/cross_platform/matched.jsonl',
        help='Output JSONL file',
    )
    parser.add_argument(
        '--min-similarity',
        type=float,
        default=MIN_SIMILARITY,
    )
    parser.add_argument('--max-matches', type=int, default=50)
    parser.add_argument('--interval', default='max', choices=['max', '1h', '6h', '1d'])
    parser.add_argument(
        '--use-pipeline',
        action='store_true',
        help='Use coinjure matching pipeline instead of legacy matcher',
    )
    args = parser.parse_args()

    print('=' * 64)
    print('  Cross-Platform Market Data Crawler')
    print('=' * 64)
    print()

    if args.use_pipeline:
        # Use coinjure multi-stage pipeline
        print('[Pipeline] Using coinjure matching pipeline...')
        matches = _run_pipeline_scan()
    else:
        # Legacy path
        # 1. Fetch markets
        poly_markets = fetch_polymarket_markets()
        kalshi_markets = fetch_kalshi_markets()

        if not poly_markets:
            print('ERROR: No Polymarket markets fetched.')
            sys.exit(1)
        if not kalshi_markets:
            print('ERROR: No Kalshi markets fetched.')
            sys.exit(1)

        # 2. Match
        matches = match_markets(
            poly_markets, kalshi_markets, min_sim=args.min_similarity
        )

    if not matches:
        print('\nNo matches found. Try lowering --min-similarity.')
        sys.exit(1)

    # 3. Fetch histories
    rows = fetch_histories_for_matches(
        matches,
        max_matches=args.max_matches,
        interval=args.interval,
    )

    # 4. Write output
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')

    # Stats
    match_ids = {r['match_id'] for r in rows}
    poly_rows = [r for r in rows if r['platform'] == 'polymarket']
    kalshi_rows = [r for r in rows if r['platform'] == 'kalshi']
    poly_pts = sum(len(r['time_series']['Yes']) for r in poly_rows)
    kalshi_pts = sum(len(r['time_series']['Yes']) for r in kalshi_rows)
    both_data = sum(
        1
        for mid in match_ids
        if any(
            r['match_id'] == mid
            and r['platform'] == 'polymarket'
            and r['time_series']['Yes']
            for r in rows
        )
        and any(
            r['match_id'] == mid
            and r['platform'] == 'kalshi'
            and r['time_series']['Yes']
            for r in rows
        )
    )

    print(f'\n{"=" * 64}')
    print(f'  Done! Wrote {len(rows)} rows to {out_path}')
    print(f'  Matched pairs:        {len(match_ids)}')
    print(f'  Both have data:       {both_data}')
    print(f'  Polymarket rows:      {len(poly_rows)} ({poly_pts} price points)')
    print(f'  Kalshi rows:          {len(kalshi_rows)} ({kalshi_pts} price points)')

    # Write match summary
    summary_path = out_path.parent / 'match_summary.json'
    summary = []
    for m in matches[: args.max_matches]:
        mid = m['match_id']
        p_pts = sum(
            len(r['time_series']['Yes'])
            for r in rows
            if r['match_id'] == mid and r['platform'] == 'polymarket'
        )
        k_pts = sum(
            len(r['time_series']['Yes'])
            for r in rows
            if r['match_id'] == mid and r['platform'] == 'kalshi'
        )
        summary.append(
            {
                'match_id': mid,
                'similarity': m['similarity'],
                'poly_question': m['poly']['question'],
                'kalshi_title': m['kalshi']['title'],
                'poly_points': p_pts,
                'kalshi_points': k_pts,
                'both_have_data': p_pts > 0 and k_pts > 0,
            }
        )
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    print(f'  Match summary:        {summary_path}')
    print(f'{"=" * 64}')


if __name__ == '__main__':
    main()
