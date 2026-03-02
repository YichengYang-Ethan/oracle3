"""On-chain data signal source for Solana.

Monitors whale wallets, large SPL transfers, and DFlow TVL changes
via Solana RPC polling.

Agent tool: get_onchain_signals(limit=10) -> list[dict]
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import httpx

from oracle3.data.data_source import DataSource
from oracle3.events.events import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OnChainSignalEvent — new event type for on-chain signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchedWallet:
    """A wallet to monitor for balance changes."""

    address: str
    label: str


@dataclass(frozen=True)
class OnChainSignal:
    """A detected on-chain signal."""

    signal_type: str  # whale_transfer, tvl_change, large_transfer
    wallet: str
    amount: float
    direction: str  # inflow, outflow, increase, decrease
    token: str
    timestamp: float
    label: str = ''


# ---------------------------------------------------------------------------
# OnChainSignalSource
# ---------------------------------------------------------------------------


class OnChainSignalSource(DataSource):
    """Polls Solana RPC for on-chain trading signals.

    Monitors:
    - Whale wallet balance changes (SOL and SPL tokens)
    - Large SPL transfers above a threshold
    - DFlow TVL changes
    """

    def __init__(
        self,
        rpc_url: str = 'https://api.mainnet-beta.solana.com',
        watched_wallets: list[WatchedWallet] | None = None,
        polling_interval: float = 30.0,
        large_transfer_threshold: float = 10_000.0,
        usdc_mint: str = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
    ) -> None:
        self.rpc_url = rpc_url
        self.watched_wallets = watched_wallets or []
        self.polling_interval = polling_interval
        self.large_transfer_threshold = large_transfer_threshold
        self.usdc_mint = usdc_mint

        self._signals: deque[OnChainSignal] = deque(maxlen=500)
        self._event_queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._wallet_balances: dict[str, Decimal] = {}
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None

    @property
    def signals(self) -> list[OnChainSignal]:
        return list(self._signals)

    def get_onchain_signals(self, limit: int = 10) -> list[dict[str, Any]]:
        """Agent tool: return recent on-chain signals.

        Args:
            limit: Maximum signals to return.

        Returns:
            List of signal dicts.
        """
        recent = list(self._signals)[-limit:]
        return [asdict(s) for s in recent]

    async def get_next_event(self) -> Event | None:
        """Return the next queued on-chain signal event."""
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    async def start(self) -> None:
        """Start the background polling loop."""
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_wallet_balances()
            except Exception:
                logger.debug('On-chain signal poll error', exc_info=True)
            await asyncio.sleep(self.polling_interval)

    async def _poll_wallet_balances(self) -> None:
        """Check whale wallet balances for changes."""
        for wallet in self.watched_wallets:
            try:
                new_balance = await self._get_spl_balance(
                    wallet.address, self.usdc_mint
                )
                old_balance = self._wallet_balances.get(wallet.address)
                self._wallet_balances[wallet.address] = new_balance

                if old_balance is not None:
                    delta = new_balance - old_balance
                    abs_delta = abs(float(delta))
                    if abs_delta >= self.large_transfer_threshold:
                        direction = 'inflow' if delta > 0 else 'outflow'
                        signal = OnChainSignal(
                            signal_type='whale_transfer',
                            wallet=wallet.address,
                            amount=abs_delta,
                            direction=direction,
                            token='USDC',
                            timestamp=time.time(),
                            label=wallet.label,
                        )
                        self._signals.append(signal)
                        logger.info(
                            'Whale signal: %s %s %.2f USDC (%s)',
                            wallet.label, direction, abs_delta, wallet.address[:8],
                        )

                        # Emit as event
                        from oracle3.events.events import OnChainSignalEvent

                        event = OnChainSignalEvent(
                            signal_type='whale_transfer',
                            wallet=wallet.address,
                            amount=Decimal(str(abs_delta)),
                            direction=direction,
                        )
                        await self._event_queue.put(event)

            except Exception:
                logger.debug(
                    'Failed to poll wallet %s', wallet.address[:8], exc_info=True
                )

    async def _get_spl_balance(self, owner: str, mint: str) -> Decimal:
        """Get SPL token balance for a wallet."""
        rpc_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'getTokenAccountsByOwner',
            'params': [
                owner,
                {'mint': mint},
                {'encoding': 'jsonParsed'},
            ],
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.rpc_url, json=rpc_payload)
            result = resp.json()

        accounts = result.get('result', {}).get('value', [])
        total = Decimal('0')
        for acct in accounts:
            info = (
                acct.get('account', {})
                .get('data', {})
                .get('parsed', {})
                .get('info', {})
            )
            amount = info.get('tokenAmount', {}).get('uiAmountString', '0')
            total += Decimal(amount)
        return total
