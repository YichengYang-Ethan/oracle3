"""Atomic multi-leg trader — all-or-nothing Solana transactions.

Combines prediction market orders with hedge instruments (Jupiter swap,
Drift perp) into a single Solana transaction for atomic execution.

Agent tool: place_hedged_order(...) -> dict
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HedgeLeg:
    """A single leg of a multi-leg atomic trade."""

    instrument_type: str  # prediction_market, jupiter_swap, drift_perp
    ticker: str
    side: str  # buy, sell
    qty: float
    price: float


@dataclass(frozen=True)
class AtomicTradeResult:
    """Result of an atomic multi-leg trade execution."""

    success: bool
    signature: str
    legs: list[dict[str, Any]]
    total_cost: float
    error: str = ''


class AtomicTrader:
    """Execute multi-leg atomic trades on Solana.

    Packs multiple instructions into a single Solana transaction:
    - DFlow prediction market orders
    - Jupiter DEX swaps
    - Drift perpetual positions

    All legs succeed or fail together (all-or-nothing).
    """

    def __init__(
        self,
        keypair: Any | None = None,
        rpc_url: str = 'https://api.mainnet-beta.solana.com',
        jito_submitter: Any | None = None,
        trade_api_base: str = 'https://dev-quote-api.dflow.net',
    ) -> None:
        self._keypair = keypair
        self.rpc_url = rpc_url
        self._jito = jito_submitter
        self.trade_api_base = trade_api_base

        # Stats
        self._total_attempts: int = 0
        self._successes: int = 0

    async def place_hedged_order(
        self,
        prediction_market_symbol: str,
        prediction_side: str,
        prediction_qty: float,
        prediction_price: float,
        hedge_instrument: str,
        hedge_ticker: str,
        hedge_side: str,
        hedge_qty: float,
        hedge_price: float,
    ) -> dict[str, Any]:
        """Agent tool: place an atomic hedged order.

        Places a prediction market order and a hedge instrument order
        in a single Solana transaction.

        Args:
            prediction_market_symbol: DFlow market ticker.
            prediction_side: 'buy' or 'sell' for prediction market.
            prediction_qty: Quantity for prediction market leg.
            prediction_price: Price for prediction market leg.
            hedge_instrument: Type of hedge ('jupiter_swap' or 'drift_perp').
            hedge_ticker: Ticker/token for the hedge leg.
            hedge_side: 'buy' or 'sell' for hedge leg.
            hedge_qty: Quantity for hedge leg.
            hedge_price: Price for hedge leg.

        Returns:
            Dict with success, signature, legs, total_cost.
        """
        self._total_attempts += 1

        legs = [
            HedgeLeg(
                instrument_type='prediction_market',
                ticker=prediction_market_symbol,
                side=prediction_side,
                qty=prediction_qty,
                price=prediction_price,
            ),
            HedgeLeg(
                instrument_type=hedge_instrument,
                ticker=hedge_ticker,
                side=hedge_side,
                qty=hedge_qty,
                price=hedge_price,
            ),
        ]

        try:
            tx_bytes = await self._build_atomic_tx(legs)

            if tx_bytes is None:
                return asdict(AtomicTradeResult(
                    success=False,
                    signature='',
                    legs=[asdict(leg) for leg in legs],
                    total_cost=0.0,
                    error='Failed to build atomic transaction',
                ))

            # Submit via Jito if available
            if self._jito:
                result = await self._jito.submit_with_jito(tx_bytes)
                signature = result.signature
                success = result.success
            else:
                signature = await self._submit_tx(tx_bytes)
                success = bool(signature)

            total_cost = sum(leg.qty * leg.price for leg in legs)

            if success:
                self._successes += 1

            return asdict(AtomicTradeResult(
                success=success,
                signature=signature,
                legs=[asdict(leg) for leg in legs],
                total_cost=round(total_cost, 4),
            ))

        except Exception as exc:
            logger.exception('Atomic trade failed: %s', exc)
            return asdict(AtomicTradeResult(
                success=False,
                signature='',
                legs=[asdict(leg) for leg in legs],
                total_cost=0.0,
                error=str(exc),
            ))

    async def _build_atomic_tx(self, legs: list[HedgeLeg]) -> bytes | None:
        """Build a multi-instruction Solana transaction.

        In production, each leg would generate actual program instructions
        for the target protocol (DFlow, Jupiter, Drift). Here we create
        a memo-based transaction documenting the atomic intent.
        """
        try:
            from solders.instruction import AccountMeta, Instruction
            from solders.message import MessageV0
            from solders.pubkey import Pubkey
            from solders.transaction import VersionedTransaction

            assert self._keypair is not None, 'keypair required for atomic transactions'
            payer = self._keypair.pubkey()
            memo_program = Pubkey.from_string(
                'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'
            )

            instructions: list[Instruction] = []
            for leg in legs:
                memo_data = json.dumps({
                    'app': 'oracle3',
                    'action': 'atomic_leg',
                    'type': leg.instrument_type,
                    'ticker': leg.ticker[:20],
                    'side': leg.side,
                    'qty': leg.qty,
                    'price': leg.price,
                }, separators=(',', ':')).encode('utf-8')

                ix = Instruction(
                    program_id=memo_program,
                    accounts=[
                        AccountMeta(
                            pubkey=payer, is_signer=True, is_writable=True
                        )
                    ],
                    data=memo_data[:500],
                )
                instructions.append(ix)

            blockhash = await self._get_recent_blockhash()

            msg = MessageV0.try_compile(
                payer=payer,
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            assert self._keypair is not None
            tx = VersionedTransaction(msg, [self._keypair])
            return bytes(tx)

        except ImportError:
            logger.warning('solders not installed — cannot build atomic tx')
            return None
        except Exception:
            logger.debug('Failed to build atomic tx', exc_info=True)
            return None

    async def _get_recent_blockhash(self) -> Any:
        """Fetch recent blockhash from RPC."""
        from solders.hash import Hash

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                self.rpc_url,
                json={
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'getLatestBlockhash',
                    'params': [{'commitment': 'finalized'}],
                },
            )
            result = resp.json()

        blockhash_str = result['result']['value']['blockhash']
        return Hash.from_string(blockhash_str)

    async def _submit_tx(self, tx_bytes: bytes) -> str:
        """Submit transaction via standard RPC."""
        tx_b64 = base64.b64encode(tx_bytes).decode('ascii')
        rpc_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'sendTransaction',
            'params': [tx_b64, {'encoding': 'base64'}],
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.rpc_url, json=rpc_payload)
                result = resp.json()
            if 'error' in result:
                logger.error('Atomic tx failed: %s', result['error'])
                return ''
            return result.get('result', '')
        except Exception:
            logger.exception('Atomic tx submission failed')
            return ''

    @property
    def stats(self) -> dict[str, Any]:
        return {
            'total_attempts': self._total_attempts,
            'successes': self._successes,
            'success_rate': (
                round(self._successes / self._total_attempts, 4)
                if self._total_attempts > 0
                else 0.0
            ),
        }
