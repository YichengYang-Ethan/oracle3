"""Solana Actions server for Oracle3 prediction market Blinks.

NOTE: Production deployment should use a reverse proxy (e.g. nginx, Caddy)
with rate limiting to prevent abuse of the trade execution endpoints.
"""

from __future__ import annotations

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

DFLOW_TRADE_API = 'https://dev-quote-api.dflow.net'
DFLOW_METADATA_API = 'https://dev-prediction-markets-api.dflow.net'

# TODO: add an actual icon at this path and update the URL
ICON_URL = 'https://raw.githubusercontent.com/YichengYang-Ethan/oracle3/main/assets/icon.png'

# --- Input validation constants ---
_MARKET_TICKER_RE = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
_MAX_AMOUNT = 10_000

# --- In-memory cache for market metadata ---
_market_cache: dict[str, tuple[float, dict]] = {}  # ticker -> (timestamp, metadata)
_CACHE_TTL = 300  # 5 minutes


def _get_cached_market(ticker: str) -> dict | None:
    """Return cached market metadata if still valid, else None."""
    entry = _market_cache.get(ticker)
    if entry is None:
        return None
    ts, data = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _market_cache[ticker]
        return None
    return data


def _set_cached_market(ticker: str, data: dict) -> None:
    """Store market metadata in cache."""
    _market_cache[ticker] = (time.monotonic(), data)


def _build_app():
    """Build and return the FastAPI application (lazy import)."""
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError(
            'FastAPI not installed. Install with: poetry install -E blinks'
        ) from exc

    class ExecuteTradeBody(BaseModel):
        """Request body for the execute_trade endpoint (Solana Actions spec)."""
        account: str = Field(
            ...,
            description='Wallet public key of the user initiating the trade',
        )

    app = FastAPI(
        title='Oracle3 Blinks',
        description='Solana Actions server for prediction market trading',
        version='1.0.0',
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_methods=['GET', 'POST', 'OPTIONS'],
        allow_headers=['*'],
    )

    @app.get('/actions.json')
    async def actions_metadata() -> JSONResponse:
        return JSONResponse({
            'rules': [
                {'pathPattern': '/api/trade/**', 'apiPath': '/api/trade/**'},
            ],
        })

    @app.get('/api/trade/{market_ticker}')
    async def get_action(market_ticker: str) -> JSONResponse:
        """Return Solana Action metadata for a prediction market."""
        if not _MARKET_TICKER_RE.match(market_ticker):
            raise HTTPException(
                status_code=400,
                detail='market_ticker must be alphanumeric/underscores/hyphens, max 100 chars',
            )

        title = f'Trade: {market_ticker}'
        description = f'Trade prediction market {market_ticker} on DFlow/Solana'

        # Check cache first
        cached = _get_cached_market(market_ticker)
        if cached is not None:
            title = cached['title']
            description = cached['description']
        else:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f'{DFLOW_METADATA_API}/api/v1/events',
                        params={'status': 'active', 'withNestedMarkets': 'true'},
                    )
                    if resp.status_code == 200:
                        body = resp.json()
                        events = body if isinstance(body, list) else body.get('events', [])
                        for event in events:
                            for mkt in event.get('markets', []):
                                ticker = mkt.get('ticker', mkt.get('marketTicker', ''))
                                if ticker == market_ticker:
                                    title = mkt.get('title', mkt.get('question', title))
                                    description = f'Trade: {title}'
                                    _set_cached_market(market_ticker, {
                                        'title': title,
                                        'description': description,
                                    })
                                    break
            except Exception as e:
                logger.warning('Failed to fetch market info for %s: %s', market_ticker, e)

        return JSONResponse({
            'type': 'action',
            'icon': ICON_URL,
            'title': title,
            'description': description,
            'label': 'Trade',
            'links': {
                'actions': [
                    {
                        'label': 'Buy YES',
                        'href': f'/api/trade/{market_ticker}/execute?side=yes&amount={{amount}}',
                        'parameters': [
                            {
                                'name': 'amount',
                                'label': 'Amount (contracts)',
                                'required': True,
                            }
                        ],
                    },
                    {
                        'label': 'Buy NO',
                        'href': f'/api/trade/{market_ticker}/execute?side=no&amount={{amount}}',
                        'parameters': [
                            {
                                'name': 'amount',
                                'label': 'Amount (contracts)',
                                'required': True,
                            }
                        ],
                    },
                ],
            },
        })

    @app.post('/api/trade/{market_ticker}/execute')
    async def execute_trade(
        market_ticker: str,
        body: ExecuteTradeBody,
        side: str = 'yes',
        amount: int = 1,
        price: float | None = Query(None, description='Limit price in dollars'),
    ) -> JSONResponse:
        """Build a transaction via DFlow Trade API and return base64 serialized tx."""
        if not _MARKET_TICKER_RE.match(market_ticker):
            raise HTTPException(
                status_code=400,
                detail='market_ticker must be alphanumeric/underscores/hyphens, max 100 chars',
            )
        if side not in ('yes', 'no'):
            raise HTTPException(status_code=400, detail='side must be yes or no')
        if amount < 1 or amount > _MAX_AMOUNT:
            raise HTTPException(
                status_code=400,
                detail=f'amount must be between 1 and {_MAX_AMOUNT}',
            )

        try:
            # Request transaction from DFlow
            payload: dict = {
                'marketTicker': market_ticker,
                'side': side,
                'count': amount,
                'owner': body.account,
            }
            if price is not None:
                # DFlow expects price in cents
                payload['price'] = int(price * 100)

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f'{DFLOW_TRADE_API}/api/v1/order',
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            tx_b64 = data.get('transaction', '')
            if not tx_b64:
                raise HTTPException(
                    status_code=502,
                    detail='DFlow API returned no transaction',
                )

            return JSONResponse({
                'transaction': tx_b64,
                'message': f'Trade {amount} {side.upper()} contracts on {market_ticker}',
            })

        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f'DFlow API error: {e.response.status_code}',
            ) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.exception('Error building trade tx: %s', e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return app


def run_server(host: str = '0.0.0.0', port: int = 8080) -> None:
    """Start the Blinks Action server."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            'uvicorn not installed. Install with: poetry install -E blinks'
        ) from exc

    app = _build_app()
    uvicorn.run(app, host=host, port=port)
