"""Solana Actions server for Oracle3 prediction market Blinks."""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DFLOW_TRADE_API = 'https://dev-quote-api.dflow.net'
DFLOW_METADATA_API = 'https://dev-prediction-markets-api.dflow.net'


def _build_app():
    """Build and return the FastAPI application (lazy import)."""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            'FastAPI not installed. Install with: poetry install -E blinks'
        ) from exc

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
        # Fetch market info from DFlow
        title = f'Trade: {market_ticker}'
        description = f'Trade prediction market {market_ticker} on DFlow/Solana'

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f'{DFLOW_METADATA_API}/api/v1/events',
                    params={'status': 'active', 'withNestedMarkets': 'true'},
                )
                if resp.status_code == 200:
                    events = resp.json() if isinstance(resp.json(), list) else resp.json().get('events', [])
                    for event in events:
                        for mkt in event.get('markets', []):
                            ticker = mkt.get('ticker', mkt.get('marketTicker', ''))
                            if ticker == market_ticker:
                                title = mkt.get('title', mkt.get('question', title))
                                description = f'Trade: {title}'
                                break
        except Exception as e:
            logger.warning('Failed to fetch market info for %s: %s', market_ticker, e)

        return JSONResponse({
            'type': 'action',
            'icon': 'https://raw.githubusercontent.com/user/oracle3/main/assets/icon.png',
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
        side: str = 'yes',
        amount: int = 1,
    ) -> JSONResponse:
        """Build a transaction via DFlow Trade API and return base64 serialized tx."""
        if side not in ('yes', 'no'):
            raise HTTPException(status_code=400, detail='side must be yes or no')
        if amount < 1:
            raise HTTPException(status_code=400, detail='amount must be >= 1')

        try:
            # Request transaction from DFlow
            payload = {
                'marketTicker': market_ticker,
                'side': side,
                'count': amount,
            }
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
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception('Error building trade tx: %s', e)
            raise HTTPException(status_code=500, detail=str(e))

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
