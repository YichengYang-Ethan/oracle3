"""Record live DFlow WebSocket data to parquet for backtesting.

Usage:
    python -m oracle3.data.backtest.dflow_recorder --duration 3600 --output data/episodes/my_session

Produces:
    {output}/dflow_events.parquet   — all events (price + orderbook)
    {output}/metadata.json          — session metadata
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import websockets

logger = logging.getLogger(__name__)

_WS_URL = 'wss://dev-prediction-markets-api.dflow.net/api/v1/ws'
_CHANNELS = ['prices', 'orderbook', 'trades']


def _safe_float(v) -> float:
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


class DFlowRecorder:
    """Records DFlow WS events to parquet files."""

    def __init__(self, output_dir: str, ws_url: str = _WS_URL) -> None:
        self.output_dir = Path(output_dir)
        self.ws_url = ws_url
        self._rows: list[dict] = []
        self._tickers_seen: set[str] = set()

    async def record(self, duration_seconds: float) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        start = time.time()
        logger.info('Recording DFlow WS data for %.0fs → %s', duration_seconds, self.output_dir)

        try:
            async with websockets.connect(self.ws_url) as ws:
                logger.info('Connected to %s', self.ws_url)
                for channel in _CHANNELS:
                    await ws.send(json.dumps({'type': 'subscribe', 'channel': channel}))

                while time.time() - start < duration_seconds:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._process_message(msg)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning('WS connection error', exc_info=True)

        self._save()

    def _process_message(self, msg: dict) -> None:
        channel = msg.get('channel', msg.get('type', ''))
        data = msg.get('data', msg)
        market_ticker = data.get('marketTicker', data.get('market_ticker', ''))
        if not market_ticker:
            return

        self._tickers_seen.add(market_ticker)
        now = datetime.now(tz=timezone.utc)

        if channel == 'prices':
            price = _safe_float(data.get('price', data.get('lastPrice', 0)))
            if price > 0:
                if price > 1:
                    price /= 100.0
                self._rows.append({
                    'ts': now, 'ticker': market_ticker,
                    'event_type': 'price', 'price': price,
                    'size': 0.0, 'side': '',
                })

        elif channel == 'orderbook':
            for side_key, side_label in [('bids', 'bid'), ('asks', 'ask')]:
                levels = data.get(side_key, [])
                if isinstance(levels, list):
                    for level in levels[:3]:
                        p, s = 0.0, 0.0
                        if isinstance(level, dict):
                            p = _safe_float(level.get('price', 0))
                            s = _safe_float(level.get('size', level.get('quantity', 100)))
                        elif isinstance(level, (list, tuple)) and len(level) >= 2:
                            p, s = _safe_float(level[0]), _safe_float(level[1])
                        if p > 0:
                            if p > 1:
                                p /= 100.0
                            self._rows.append({
                                'ts': now, 'ticker': market_ticker,
                                'event_type': 'orderbook', 'price': p,
                                'size': s, 'side': side_label,
                            })

        elif channel == 'trades':
            price = _safe_float(data.get('price', 0))
            size = _safe_float(data.get('size', data.get('quantity', 0)))
            if price > 0:
                if price > 1:
                    price /= 100.0
                self._rows.append({
                    'ts': now, 'ticker': market_ticker,
                    'event_type': 'trade', 'price': price,
                    'size': size, 'side': data.get('side', ''),
                })

    def _save(self) -> None:
        if not self._rows:
            logger.warning('No events recorded')
            return

        df = pd.DataFrame(self._rows)
        parquet_path = self.output_dir / 'dflow_events.parquet'
        df.to_parquet(parquet_path, index=False)

        meta = {
            'episode_id': self.output_dir.name,
            'source': 'dflow_ws',
            'ws_url': self.ws_url,
            'tickers': sorted(self._tickers_seen),
            'start_ts': df['ts'].min().isoformat(),
            'end_ts': df['ts'].max().isoformat(),
            'total_events': len(df),
        }
        meta_path = self.output_dir / 'metadata.json'
        meta_path.write_text(json.dumps(meta, indent=2, default=str))

        logger.info(
            'Saved %d events (%d tickers) to %s',
            len(df), len(self._tickers_seen), parquet_path,
        )


async def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Record DFlow WS data')
    parser.add_argument('--duration', type=float, default=300, help='Seconds to record')
    parser.add_argument('--output', default='data/episodes/dflow_session', help='Output dir')
    args = parser.parse_args()

    recorder = DFlowRecorder(args.output)
    await recorder.record(args.duration)


if __name__ == '__main__':
    asyncio.run(_main())
