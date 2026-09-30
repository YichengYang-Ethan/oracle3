"""Kalshi <-> PredictIt cross-venue arbitrage feed as an Oracle3 data source.

The feed is an external, paid HTTP API (x402 v2 ``exact`` scheme, USDC on
Base) that entity-resolves Kalshi markets against PredictIt contracts and
reports the fee-adjusted worst-case net yield of each pair. Oracle3 cannot
trade PredictIt, so the feed is surfaced as ``NewsEvent`` signals that any
strategy (for example the LLM/news strategies) can consume.

Demo mode (default): with no wallet key nothing touches the network and the
scanner returns ``data/kalshi_predictit_arb_sample.json``, which is fictional
SAMPLE data, not real market data.

Live mode: only when a key is passed or ``X402_WALLET_KEY`` is set. Each call
signs an EIP-3009 USDC authorization of at most ``max_usd_per_call``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from oracle3.data.data_source import DataSource
from oracle3.events.events import Event, NewsEvent
from oracle3.strategy.strategy import Strategy
from oracle3.trader.trader import Trader

logger = logging.getLogger(__name__)

DEFAULT_URL = (
    'https://x402.bankr.bot/0x69fb671637ed68881f66b9ebf305ec3ef5574f65'
    '/kalshi-predictit-arb'
)
BASE_NETWORKS = ('eip155:8453', 'base')
BASE_CHAIN_ID = 8453
BASE_USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
USDC_DECIMALS = 6
MODES = ('opportunities', 'all')
SOURCE = 'kalshi-predictit-arb'
SAMPLE_FIXTURE = (
    Path(__file__).resolve().parents[2] / 'data' / 'kalshi_predictit_arb_sample.json'
)

TRANSFER_WITH_AUTHORIZATION_TYPES = {
    'TransferWithAuthorization': [
        {'name': 'from', 'type': 'address'},
        {'name': 'to', 'type': 'address'},
        {'name': 'value', 'type': 'uint256'},
        {'name': 'validAfter', 'type': 'uint256'},
        {'name': 'validBefore', 'type': 'uint256'},
        {'name': 'nonce', 'type': 'bytes32'},
    ]
}


class ArbScannerError(Exception):
    """Base error for the arbitrage feed client."""


class PaymentRefused(ArbScannerError):
    """No acceptable payment requirement (network, scheme, asset, amount, cap)."""


class PaymentFailed(ArbScannerError):
    """A payment was sent but the server still answered 402."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ArbScanner:
    """Synchronous client for the feed; offline demo mode without a key."""

    def __init__(
        self,
        private_key: str | bytes | None = None,
        max_usd_per_call: float | str = 0.02,
        timeout: float = 15.0,
        url: str = DEFAULT_URL,
        fixture_path: str | Path = SAMPLE_FIXTURE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        private_key = private_key or os.environ.get('X402_WALLET_KEY')
        self._account: Any = None
        if private_key:
            from eth_account import Account  # only needed in live mode

            self._account = Account.from_key(private_key)
        self.max_atomic = int(Decimal(str(max_usd_per_call)) * 10**USDC_DECIMALS)
        self.timeout = timeout
        self.url = url
        self.fixture_path = Path(fixture_path)
        self.transport = transport
        self.last_payment_response: dict[str, Any] | None = None

    @property
    def demo(self) -> bool:
        return self._account is None

    def scan(
        self, q: str | None = None, limit: int = 10, mode: str = 'opportunities'
    ) -> dict[str, Any]:
        if not 1 <= int(limit) <= 25:
            raise ValueError('limit must be between 1 and 25')
        if mode not in MODES:
            raise ValueError(f'mode must be one of {MODES}')
        if self.demo:
            return self._scan_fixture(q, int(limit), mode)

        params: dict[str, Any] = {'limit': int(limit), 'mode': mode}
        if q:
            params['q'] = q
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            response = client.get(self.url, params=params)
            if response.status_code == 402:
                required = parse_payment_required(response)
                requirement = select_requirement(
                    required.get('accepts'), self.max_atomic
                )
                header = build_payment_header(
                    self._account,
                    requirement,
                    required.get('resource') or {'url': self.url},
                )
                response = client.get(
                    self.url,
                    params=params,
                    headers={'PAYMENT-SIGNATURE': header, 'X-PAYMENT': header},
                )
                if response.status_code == 402:
                    raise PaymentFailed(f'Payment not accepted: {response.text[:200]}')
                self.last_payment_response = _b64_json(
                    response.headers.get('PAYMENT-RESPONSE')
                )
            response.raise_for_status()
            return normalise_response(response.json())

    def _scan_fixture(self, q: str | None, limit: int, mode: str) -> dict[str, Any]:
        data = normalise_response(json.loads(self.fixture_path.read_text()))
        opportunities = data['opportunities']
        if mode == 'opportunities':
            opportunities = [o for o in opportunities if is_executable(o)]
        if q:
            words = q.lower().split()
            opportunities = [
                o
                for o in opportunities
                if all(w in json.dumps(o).lower() for w in words)
            ]
        data['opportunities'] = opportunities[:limit]
        data['sample_data'] = True
        return data


def normalise_response(data: Any) -> dict[str, Any]:
    """Tolerate missing or unknown fields: always a list of dict opportunities."""
    if not isinstance(data, dict):
        data = {}
    opportunities = data.get('opportunities')
    if not isinstance(opportunities, list):
        opportunities = []
    data['opportunities'] = [o for o in opportunities if isinstance(o, dict)]
    return data


def net_yield(opportunity: dict[str, Any], key: str = 'net_yield_c') -> float | None:
    best = opportunity.get('best_direction')
    if not isinstance(best, dict):
        return None
    try:
        return float(best[key])
    except (KeyError, TypeError, ValueError):
        return None


def is_executable(opportunity: dict[str, Any]) -> bool:
    return opportunity.get('executable') is True


# ---------------------------------------------------------------------------
# x402 v2 payment
# ---------------------------------------------------------------------------


def parse_payment_required(response: httpx.Response) -> dict[str, Any]:
    """x402 v2 sends PaymentRequired base64 in PAYMENT-REQUIRED; else JSON body."""
    required = _b64_json(response.headers.get('PAYMENT-REQUIRED'))
    if required is None:
        try:
            required = response.json()
        except ValueError:
            required = None
    if not isinstance(required, dict):
        raise PaymentRefused('402 response without payment requirements')
    return required


def select_requirement(accepts: Any, max_atomic: int) -> dict[str, Any]:
    if not accepts or not isinstance(accepts, list):
        raise PaymentRefused('402 response offered no payment requirements')
    reasons = []
    for requirement in accepts:
        reason = _refusal_reason(requirement, max_atomic)
        if reason is None:
            return requirement
        reasons.append(reason)
    raise PaymentRefused('; '.join(reasons))


def _refusal_reason(requirement: Any, max_atomic: int) -> str | None:
    if not isinstance(requirement, dict):
        return 'malformed requirement'
    if requirement.get('network') not in BASE_NETWORKS:
        return f'network {requirement.get("network")!r} is not Base'
    if requirement.get('scheme') != 'exact':
        return f'scheme {requirement.get("scheme")!r} is not exact'
    if str(requirement.get('asset', '')).lower() != BASE_USDC.lower():
        return f'asset {requirement.get("asset")!r} is not Base USDC'
    # v2 uses 'amount', v1 used 'maxAmountRequired'
    raw = requirement.get('amount', requirement.get('maxAmountRequired'))
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return f'invalid amount {raw!r}'
    if amount <= 0:
        return f'amount {amount} must be positive'
    if amount > max_atomic:
        return f'amount {amount} exceeds cap {max_atomic}'
    pay_to = requirement.get('payTo')
    if not (isinstance(pay_to, str) and pay_to.startswith('0x') and len(pay_to) == 42):
        return f'invalid payTo {pay_to!r}'
    return None


def build_typed_data(
    requirement: dict[str, Any], authorization: dict[str, Any]
) -> dict[str, Any]:
    extra = requirement.get('extra') or {}
    return {
        'types': {
            'EIP712Domain': [
                {'name': 'name', 'type': 'string'},
                {'name': 'version', 'type': 'string'},
                {'name': 'chainId', 'type': 'uint256'},
                {'name': 'verifyingContract', 'type': 'address'},
            ],
            **TRANSFER_WITH_AUTHORIZATION_TYPES,
        },
        'primaryType': 'TransferWithAuthorization',
        'domain': {
            'name': extra.get('name') or 'USD Coin',
            'version': extra.get('version') or '2',
            'chainId': BASE_CHAIN_ID,
            'verifyingContract': requirement['asset'],
        },
        'message': authorization,
    }


def build_payment_header(
    account: Any,
    requirement: dict[str, Any],
    resource: Any,
    now: int | None = None,
) -> str:
    from eth_account.messages import encode_typed_data

    now = int(time.time()) if now is None else now
    amount = int(requirement.get('amount', requirement.get('maxAmountRequired')))
    authorization = {
        'from': account.address,
        'to': requirement['payTo'],
        'value': amount,
        'validAfter': now - 600,
        'validBefore': now + int(requirement.get('maxTimeoutSeconds') or 60),
        'nonce': secrets.token_bytes(32),
    }
    signed = account.sign_message(
        encode_typed_data(full_message=build_typed_data(requirement, authorization))
    )
    payload = {
        'x402Version': 2,
        'resource': resource,
        'accepted': requirement,
        'payload': {
            # hexbytes>=1.0 .hex() drops the 0x prefix
            'signature': '0x' + bytes(signed.signature).hex(),
            'authorization': {
                'from': authorization['from'],
                'to': authorization['to'],
                'value': str(authorization['value']),
                'validAfter': str(authorization['validAfter']),
                'validBefore': str(authorization['validBefore']),
                'nonce': '0x' + authorization['nonce'].hex(),
            },
        },
    }
    # base64url without padding, as used by the live-tested client
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode())
        .decode()
        .rstrip('=')
    )


def _b64_json(value: str | None) -> Any:
    """Tolerant decode: accepts standard or urlsafe base64, padded or not."""
    if not value:
        return None
    try:
        value = value.strip().replace('-', '+').replace('_', '/')
        return json.loads(
            base64.b64decode(value + '=' * (-len(value) % 4), validate=True)
        )
    except (ValueError, TypeError):
        return None
    try:
        return json.loads(base64.b64decode(value + '=' * (-len(value) % 4)))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------


class KalshiPredictItArbDataSource(DataSource):
    """Polls the feed and emits one ``NewsEvent`` per new or changed pair.

    ``max_polls`` stops polling after N calls; once the queue is drained
    ``get_next_event`` returns ``None`` so a non-continuous engine ends.
    """

    def __init__(
        self,
        scanner: ArbScanner | None = None,
        q: str | None = None,
        limit: int = 10,
        mode: str = 'opportunities',
        polling_interval: float = 300.0,
        max_polls: int | None = None,
    ) -> None:
        self.scanner = scanner or ArbScanner()
        self.q = q
        self.limit = limit
        self.mode = mode
        self.polling_interval = polling_interval
        self.max_polls = max_polls
        self.polls = 0
        self.event_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)
        self._poll_task: asyncio.Task | None = None
        self._seen: dict[str, float | None] = {}

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def get_next_event(self) -> Event | None:
        if self.event_queue.empty() and self._exhausted():
            return None
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    def _exhausted(self) -> bool:
        return self.max_polls is not None and self.polls >= self.max_polls

    async def _poll_loop(self) -> None:
        while not self._exhausted():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.polls += 1
                logger.warning('Arb feed poll failed', exc_info=True)
            if not self._exhausted():
                await asyncio.sleep(self.polling_interval)

    async def poll_once(self) -> list[NewsEvent]:
        response = await asyncio.to_thread(
            self.scanner.scan, q=self.q, limit=self.limit, mode=self.mode
        )
        events = self._to_events(response)
        for event in events:
            try:
                self.event_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning('Arb feed event queue full, dropping %s', event.title)
        self.polls += 1
        return events

    def _to_events(self, response: dict[str, Any]) -> list[NewsEvent]:
        sample = bool(response.get('sample_data'))
        events = []
        for opportunity in response['opportunities']:
            pair = str(opportunity.get('pair') or '')
            if not pair:
                continue
            yield_c = net_yield(opportunity)
            if pair in self._seen and self._seen[pair] == yield_c:
                continue
            self._seen[pair] = yield_c
            yield_str = 'n/a' if yield_c is None else f'{yield_c:.2f}c'
            pct = net_yield(opportunity, 'net_yield_pct')
            pct_str = '' if pct is None else f' ({pct:.2f}%)'
            state = 'executable' if is_executable(opportunity) else 'not executable'
            prefix = '[SAMPLE DATA] ' if sample else ''
            events.append(
                NewsEvent(
                    news=f'{prefix}Cross-venue arb {pair}: worst-case net '
                    f'{yield_str}{pct_str}, {state}',
                    title=f'{prefix}{pair}',
                    source=SOURCE,
                    categories=['cross_venue_arb', 'kalshi', 'predictit'],
                    description=json.dumps(opportunity, default=str),
                    event_id=pair,
                )
            )
        return events


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class ArbFeedSignalStrategy(Strategy):
    """Records feed signals above a net-yield threshold; places no orders."""

    name = 'kalshi_predictit_arb_signal'
    version = '0.1.0'

    def __init__(self, min_net_yield_c: float = 1.0) -> None:
        super().__init__()
        self.min_net_yield_c = min_net_yield_c

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, NewsEvent) or event.source != SOURCE:
            return
        try:
            opportunity = json.loads(event.description)
        except ValueError:
            return
        if not isinstance(opportunity, dict):
            return
        yield_c = net_yield(opportunity)
        actionable = (
            is_executable(opportunity)
            and yield_c is not None
            and yield_c >= self.min_net_yield_c
        )
        signal_values = {'net_yield_c': yield_c} if yield_c is not None else {}
        self.record_decision(
            ticker_name=event.event_id,
            action='ARB_SIGNAL' if actionable else 'HOLD',
            executed=False,
            reasoning=event.news,
            signal_values=signal_values,
        )
