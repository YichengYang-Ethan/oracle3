from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import PositionManager
from oracle3.risk.risk_manager import RiskManager
from oracle3.ticker.ticker import CashTicker, SolanaTicker, Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import (
    Order,
    OrderFailureReason,
    OrderStatus,
    PlaceOrderResult,
    Trade,
    TradeSide,
)

if TYPE_CHECKING:
    from oracle3.alerts.alerter import Alerter

logger = logging.getLogger(__name__)

# Solana mainnet USDC mint address
USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'

DEFAULT_TRADE_API_BASE = 'https://dev-quote-api.dflow.net'


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Return True for transient HTTP errors (5xx, timeouts)."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _load_keypair(
    keypair: Any | None = None,
    keypair_path: str | None = None,
) -> Any:
    """Load a Solana keypair from arg, file, or env var."""
    from solders.keypair import Keypair

    if keypair is not None:
        return keypair

    path = keypair_path or os.environ.get('SOLANA_KEYPAIR_PATH')
    if path:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return Keypair.from_bytes(bytes(data))
        raise ValueError(f'Unsupported keypair format in {path}')

    raw = os.environ.get('SOLANA_PRIVATE_KEY')
    if raw:
        try:
            key_bytes = base64.b58decode(raw)
        except Exception:
            key_bytes = base64.b64decode(raw)
        return Keypair.from_bytes(key_bytes)

    raise ValueError(
        'Solana keypair required. Pass keypair, keypair_path, '
        'or set SOLANA_KEYPAIR_PATH / SOLANA_PRIVATE_KEY env var.'
    )


class SolanaTrader(Trader):
    """Trader that executes on Solana via DFlow Trade API."""

    def __init__(
        self,
        market_data: MarketDataManager,
        risk_manager: RiskManager,
        position_manager: PositionManager,
        keypair: Any | None = None,
        keypair_path: str | None = None,
        rpc_url: str = 'https://api.mainnet-beta.solana.com',
        commission_rate: Decimal = Decimal('0.0'),
        alerter: Alerter | None = None,
        trade_api_base: str = DEFAULT_TRADE_API_BASE,
        use_jito: bool = False,
        jito_tip_lamports: int = 10_000,
    ):
        super().__init__(market_data, risk_manager, position_manager, alerter=alerter)
        self.commission_rate = commission_rate
        self.rpc_url = rpc_url
        self.trade_api_base = trade_api_base
        self._keypair = _load_keypair(keypair, keypair_path)
        self.orders: list[Order] = []

        # Jito MEV protection
        self.use_jito = use_jito
        self._jito_submitter: Any | None = None
        if use_jito:
            from oracle3.trader.jito_submitter import JitoSubmitter

            self._jito_submitter = JitoSubmitter(
                keypair=self._keypair,
                rpc_url=rpc_url,
                tip_lamports=jito_tip_lamports,
            )

    @property
    def public_key(self) -> str:
        return str(self._keypair.pubkey())

    @retry(
        retry=retry_if_exception(_is_retryable_http_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _request_trade_tx(
        self,
        side: str,
        ticker: SolanaTicker,
        price_cents: int,
        quantity: int,
        action: str = 'buy',
    ) -> bytes:
        """Request a ready-to-sign transaction from DFlow Trade API."""
        payload = {
            'marketTicker': ticker.market_ticker,
            'side': side,
            'action': action,
            'price': price_cents,
            'count': quantity,
            'owner': self.public_key,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f'{self.trade_api_base}/api/v1/order',
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        tx_b64 = data.get('transaction', '')
        if not tx_b64:
            raise ValueError(f'DFlow API returned no transaction: {data}')
        return base64.b64decode(tx_b64)

    async def _sign_and_submit(self, tx_bytes: bytes) -> str:
        """Deserialize, sign, and submit a Solana transaction."""
        from solders.transaction import VersionedTransaction

        tx = VersionedTransaction.from_bytes(tx_bytes)

        # Sign the transaction
        signed_tx = VersionedTransaction(tx.message, [self._keypair])
        raw = bytes(signed_tx)

        # Pre-submission risk hook: simulate transaction if OnChainRiskManager
        from oracle3.risk.onchain_risk_manager import OnChainRiskManager

        if isinstance(self.risk_manager, OnChainRiskManager):
            sim_ok = await self.risk_manager.simulate_transaction(raw)
            if not sim_ok:
                raise RuntimeError('Transaction simulation failed — risk check blocked submission')

        # Submit via Jito if enabled, otherwise standard RPC
        if self._jito_submitter is not None:
            result = await self._jito_submitter.submit_with_jito(raw)
            if result.success:
                logger.info('Transaction submitted via Jito: %s (bundle=%s)', result.signature, result.bundle_id)
                return result.signature
            logger.warning('Jito submission failed, signature from fallback: %s', result.signature)
            return result.signature

        # Standard RPC submission
        tx_b64 = base64.b64encode(raw).decode('ascii')
        rpc_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'sendTransaction',
            'params': [
                tx_b64,
                {'encoding': 'base64', 'skipPreflight': False},
            ],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.rpc_url, json=rpc_payload)
            resp.raise_for_status()
            result = resp.json()

        if 'error' in result:
            raise RuntimeError(f'Solana RPC error: {result["error"]}')

        signature = result.get('result', '')
        logger.info('Transaction submitted: %s', signature)
        return signature

    async def _confirm_transaction(self, signature: str, timeout: float = 30.0) -> bool:
        """Poll Solana RPC for transaction confirmation."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            rpc_payload = {
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'getSignatureStatuses',
                'params': [[signature]],
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.rpc_url, json=rpc_payload)
                result = resp.json()

            statuses = result.get('result', {}).get('value', [])
            if statuses and statuses[0] is not None:
                status = statuses[0]
                if status.get('err') is not None:
                    logger.error('Transaction failed: %s', status['err'])
                    return False
                confirmation = status.get('confirmationStatus', '')
                if confirmation in ('confirmed', 'finalized'):
                    return True

            await asyncio.sleep(1.0)

        logger.warning('Transaction confirmation timed out: %s', signature)
        return False

    async def _get_spl_balance(self, mint: str) -> Decimal:
        """Check SPL token balance for a given mint."""
        rpc_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'getTokenAccountsByOwner',
            'params': [
                self.public_key,
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
            info = acct.get('account', {}).get('data', {}).get('parsed', {}).get('info', {})
            amount = info.get('tokenAmount', {}).get('uiAmountString', '0')
            total += Decimal(amount)
        return total

    async def reconcile_balance(self) -> Decimal:
        """Fetch on-chain USDC balance and update the cash position.

        Returns the reconciled balance. Intended to be called on startup
        by live_trader.py to sync with actual wallet state.
        """
        from oracle3.position.position_manager import Position

        balance = await self._get_spl_balance(USDC_MINT)
        self.position_manager.update_position(
            Position(
                ticker=CashTicker.DFLOW_USDC,
                quantity=balance,
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        logger.info('Reconciled USDC balance: %s', balance)
        return balance

    async def _alert_rejected(self, reason: OrderFailureReason, ticker: Ticker) -> None:
        if self.alerter:
            try:
                await self.alerter.on_order_rejected(reason, ticker)
            except Exception:
                logger.debug('alerter.on_order_rejected() failed', exc_info=True)

    async def place_order(
        self,
        side: TradeSide,
        ticker: Ticker,
        limit_price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> PlaceOrderResult:
        guard_failure = self._check_order_guard(client_order_id)
        if guard_failure is not None:
            await self._alert_rejected(guard_failure, ticker)
            return PlaceOrderResult(order=None, failure_reason=guard_failure)

        if quantity <= 0 or limit_price <= 0:
            await self._alert_rejected(OrderFailureReason.INVALID_ORDER, ticker)
            return PlaceOrderResult(
                order=None, failure_reason=OrderFailureReason.INVALID_ORDER
            )

        if not isinstance(ticker, SolanaTicker) or not ticker.market_ticker:
            await self._alert_rejected(OrderFailureReason.INVALID_ORDER, ticker)
            return PlaceOrderResult(
                order=None, failure_reason=OrderFailureReason.INVALID_ORDER
            )

        # No short selling without position
        if side == TradeSide.SELL:
            position = self.position_manager.get_position(ticker)
            if position is None or position.quantity < quantity:
                await self._alert_rejected(OrderFailureReason.INVALID_ORDER, ticker)
                return PlaceOrderResult(
                    order=None, failure_reason=OrderFailureReason.INVALID_ORDER
                )

        # Cash check
        if side == TradeSide.BUY:
            cash_position = self.position_manager.get_position(ticker.collateral)
            cash_required = quantity * limit_price * (Decimal('1') + self.commission_rate)
            if cash_position is None or cash_position.quantity < cash_required:
                logger.warning(
                    'Insufficient cash for %s: need %s, have %s',
                    ticker.symbol,
                    cash_required,
                    cash_position.quantity if cash_position else 0,
                )
                await self._alert_rejected(OrderFailureReason.INSUFFICIENT_CASH, ticker)
                return PlaceOrderResult(
                    order=None, failure_reason=OrderFailureReason.INSUFFICIENT_CASH
                )

        # Risk check
        if not await self.risk_manager.check_trade(ticker, side, quantity, limit_price):
            await self._alert_rejected(OrderFailureReason.RISK_CHECK_FAILED, ticker)
            return PlaceOrderResult(
                order=None, failure_reason=OrderFailureReason.RISK_CHECK_FAILED
            )

        try:
            price_cents = int(limit_price * 100)
            if quantity != int(quantity):
                raise ValueError(
                    f'Fractional quantity {quantity} not supported; '
                    f'must be a whole number'
                )
            count = int(quantity)

            api_side = 'no' if ticker.is_no_side else 'yes'
            action = 'buy' if side == TradeSide.BUY else 'sell'

            tx_bytes = await self._request_trade_tx(
                side=api_side,
                ticker=ticker,
                price_cents=price_cents,
                quantity=count,
                action=action,
            )

            signature = await self._sign_and_submit(tx_bytes)
            confirmed = await self._confirm_transaction(signature)

            if not confirmed:
                return PlaceOrderResult(
                    order=None, failure_reason=OrderFailureReason.UNKNOWN
                )

            filled_quantity = Decimal(str(count))
            commission = filled_quantity * limit_price * self.commission_rate

            trade = Trade(
                side=side,
                ticker=ticker,
                price=limit_price,
                quantity=filled_quantity,
                commission=commission,
            )

            order = Order(
                status=OrderStatus.FILLED,
                side=side,
                ticker=ticker,
                limit_price=limit_price,
                filled_quantity=filled_quantity,
                average_price=limit_price,
                trades=[trade],
                remaining=Decimal('0'),
                commission=commission,
            )

            for t in order.trades:
                self.position_manager.apply_trade(t)

            self.orders.append(order)

            # Log trade on-chain via Memo program (best-effort, non-blocking)
            try:
                from oracle3.onchain.logger import OnChainLogger

                on_chain = OnChainLogger(keypair=self._keypair, rpc_url=self.rpc_url)
                await on_chain.log_trade(
                    market_ticker=ticker.market_ticker,
                    side=action,
                    price=float(limit_price),
                    quantity=count,
                    trade_signature=signature,
                )
            except Exception as log_err:
                logger.warning('On-chain trade log failed (non-fatal): %s', log_err)

            return PlaceOrderResult(order=order)

        except Exception as e:
            logger.exception('Error placing Solana order for %s: %s', ticker.symbol, e)
            return PlaceOrderResult(
                order=None, failure_reason=OrderFailureReason.UNKNOWN
            )
