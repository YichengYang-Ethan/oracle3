"""Local paper-trading ledger for the MCP server.

Orders are filled against a snapshot of the displayed order book: the ask
levels are walked from the best price up to the limit price, each level fill
is charged the venue fee, and the result is written to a JSON ledger. Nothing
is sent to any venue. Only buys are supported, which is all a static
arbitrage basket needs; positions are carried at cost and resolution is not
tracked.

The ledger lives at ``~/.oracle3/mcp_paper_ledger.json`` unless the
``ORACLE3_MCP_LEDGER`` environment variable points elsewhere.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from oracle3.fees import FeeSchedule

DEFAULT_INITIAL_CASH = 10_000.0


def ledger_path() -> Path:
    override = os.environ.get('ORACLE3_MCP_LEDGER')
    if override:
        return Path(override).expanduser()
    return Path.home() / '.oracle3' / 'mcp_paper_ledger.json'


class PaperLedger:
    def __init__(
        self, path: Path | None = None, initial_cash: float = DEFAULT_INITIAL_CASH
    ) -> None:
        self.path = path or ledger_path()
        self.initial_cash = initial_cash
        self.state = self._load()

    def _empty(self) -> dict[str, Any]:
        return {
            'initial_cash': self.initial_cash,
            'cash': self.initial_cash,
            'positions': {},
            'fills': [],
        }

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            with open(self.path) as f:
                data: dict[str, Any] = json.load(f)
            return data
        return self._empty()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(self.state, f, indent=2)
        tmp.replace(self.path)

    def reset(self) -> dict[str, Any]:
        self.state = self._empty()
        self._save()
        return self.portfolio()

    def buy(
        self,
        *,
        venue: str,
        market_id: str,
        side: str,
        contracts: float,
        limit_price: float,
        asks: list[tuple[float, float]],
        schedule: FeeSchedule,
    ) -> dict[str, Any]:
        """Fill up to ``contracts`` against ``asks`` at or below ``limit_price``."""
        if side not in ('yes', 'no'):
            raise ValueError("side must be 'yes' or 'no'")
        if contracts <= 0:
            raise ValueError('contracts must be positive')
        if not 0.0 < limit_price < 1.0:
            raise ValueError('limit_price must be strictly between 0 and 1')

        remaining = contracts
        fills: list[dict[str, float]] = []
        for price, size in sorted(asks):
            if remaining <= 0 or price > limit_price:
                break
            qty = min(size, remaining)
            fee = float(schedule.fee(price, Decimal(str(qty))))
            fills.append({'price': price, 'contracts': qty, 'fee': fee})
            remaining -= qty

        filled = contracts - remaining
        if filled <= 0:
            return {
                'status': 'unfilled',
                'reason': f'no displayed ask at or below {limit_price}',
                'best_ask': min((p for p, _ in asks), default=None),
            }

        cost = round(sum(f['price'] * f['contracts'] for f in fills), 6)
        fees = round(sum(f['fee'] for f in fills), 6)
        if cost + fees > self.state['cash'] + 1e-9:
            return {
                'status': 'rejected',
                'reason': f"insufficient paper cash: need {cost + fees:.4f}, have {self.state['cash']:.4f}",
            }

        key = f'{venue}:{market_id}:{side}'
        position = self.state['positions'].setdefault(
            key,
            {
                'venue': venue,
                'market_id': market_id,
                'side': side,
                'contracts': 0.0,
                'cost': 0.0,
                'fees': 0.0,
            },
        )
        position['contracts'] = round(position['contracts'] + filled, 6)
        position['cost'] = round(position['cost'] + cost, 6)
        position['fees'] = round(position['fees'] + fees, 6)
        self.state['cash'] = round(self.state['cash'] - cost - fees, 6)
        record = {
            'time': datetime.now(timezone.utc).isoformat(),
            'venue': venue,
            'market_id': market_id,
            'side': side,
            'requested': contracts,
            'filled': filled,
            'limit_price': limit_price,
            'average_price': round(cost / filled, 6),
            'cost': cost,
            'fees': fees,
            'levels': fills,
        }
        self.state['fills'].append(record)
        self._save()
        return {'status': 'filled' if remaining <= 0 else 'partially_filled', **record}

    def portfolio(self) -> dict[str, Any]:
        positions = list(self.state['positions'].values())
        return {
            'ledger': str(self.path),
            'initial_cash': self.state['initial_cash'],
            'cash': self.state['cash'],
            'positions': positions,
            'fills': len(self.state['fills']),
            'note': 'Paper ledger. Positions are carried at cost; resolution and P&L are not tracked.',
        }
