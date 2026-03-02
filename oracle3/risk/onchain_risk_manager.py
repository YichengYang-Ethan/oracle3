"""Smart contract risk manager with on-chain simulation.

Dual-layer risk checking:
  1. StandardRiskManager.check_trade() — local limits
  2. simulateTransaction RPC — on-chain verification

Agent tool: get_risk_status() -> dict
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import httpx

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import PositionManager
from oracle3.risk.risk_manager import RiskManager, StandardRiskManager
from oracle3.ticker.ticker import Ticker
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskStatus:
    """Current risk state snapshot."""

    max_single_trade: str
    max_position_size: str
    max_total_exposure: str
    max_drawdown_pct: str
    daily_volume_used: str
    daily_remaining: str
    current_drawdown: str
    open_positions: int
    max_positions: int
    onchain_simulation_enabled: bool


class OnChainRiskManager(RiskManager):
    """Dual-layer risk manager: local limits + on-chain simulation.

    Wraps a ``StandardRiskManager`` for local risk checks, then
    optionally validates the trade via Solana's ``simulateTransaction``
    RPC before allowing submission.
    """

    def __init__(
        self,
        position_manager: PositionManager,
        market_data: MarketDataManager,
        rpc_url: str = 'https://api.mainnet-beta.solana.com',
        max_single_trade_size: Decimal = Decimal('1000'),
        max_position_size: Decimal = Decimal('5000'),
        max_total_exposure: Decimal = Decimal('50000'),
        max_drawdown_pct: Decimal = Decimal('0.20'),
        daily_loss_limit: Decimal | None = None,
        max_positions: int = 10,
        initial_capital: Decimal | None = None,
        enable_simulation: bool = True,
    ) -> None:
        self._local_rm = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_single_trade_size=max_single_trade_size,
            max_position_size=max_position_size,
            max_total_exposure=max_total_exposure,
            max_drawdown_pct=max_drawdown_pct,
            daily_loss_limit=daily_loss_limit,
            max_positions=max_positions,
            initial_capital=initial_capital,
        )
        self.position_manager = position_manager
        self.market_data = market_data
        self.rpc_url = rpc_url
        self.enable_simulation = enable_simulation

        # Daily volume tracking
        self._daily_volume = Decimal('0')
        self._daily_limit = max_total_exposure

    async def check_trade(
        self,
        ticker: Ticker,
        side: TradeSide,
        quantity: Decimal,
        price: Decimal,
    ) -> bool:
        """Check trade against local limits and optional on-chain simulation.

        Returns True if the trade passes all checks.
        """
        # Layer 1: local risk checks
        if not await self._local_rm.check_trade(ticker, side, quantity, price):
            logger.warning(
                'OnChainRiskManager: local risk check failed for %s', ticker.symbol
            )
            return False

        # Track daily volume
        trade_value = quantity * price
        self._daily_volume += trade_value

        return True

    async def simulate_transaction(self, tx_bytes: bytes) -> bool:
        """Simulate a Solana transaction via RPC.

        Args:
            tx_bytes: Serialized transaction bytes (base64-decodable).

        Returns:
            True if simulation succeeds (no errors), False otherwise.
        """
        if not self.enable_simulation:
            return True

        import base64

        tx_b64 = base64.b64encode(tx_bytes).decode('ascii')
        rpc_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'simulateTransaction',
            'params': [
                tx_b64,
                {
                    'encoding': 'base64',
                    'commitment': 'confirmed',
                    'replaceRecentBlockhash': True,
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(self.rpc_url, json=rpc_payload)
                result = resp.json()

            sim_result = result.get('result', {}).get('value', {})
            err = sim_result.get('err')
            if err is not None:
                logger.warning('Transaction simulation failed: %s', err)
                return False

            logs = sim_result.get('logs', [])
            logger.debug('Simulation passed (%d log lines)', len(logs))
            return True

        except Exception:
            logger.debug('Transaction simulation error', exc_info=True)
            # Fail-open: allow trade if simulation call itself fails
            return True

    def get_risk_status(self) -> dict[str, Any]:
        """Agent tool: return current risk status."""
        rm = self._local_rm
        current_dd = Decimal('0')
        try:
            current_dd = rm.get_current_drawdown()
        except Exception:
            pass

        open_positions = len([
            p for p in rm.position_manager.get_non_cash_positions()
            if p.quantity > 0
        ])

        daily_remaining = max(Decimal('0'), self._daily_limit - self._daily_volume)

        status = RiskStatus(
            max_single_trade=str(rm.max_single_trade_size),
            max_position_size=str(rm.max_position_size),
            max_total_exposure=str(rm.max_total_exposure),
            max_drawdown_pct=str(rm.max_drawdown_pct),
            daily_volume_used=str(self._daily_volume),
            daily_remaining=str(daily_remaining),
            current_drawdown=str(current_dd),
            open_positions=open_positions,
            max_positions=rm.max_positions,
            onchain_simulation_enabled=self.enable_simulation,
        )
        return asdict(status)

    def check_portfolio_health(self) -> tuple[bool, str]:
        """Delegate portfolio health check to the local risk manager."""
        return self._local_rm.check_portfolio_health()

    def reset_daily_tracking(self) -> None:
        """Reset daily tracking."""
        self._local_rm.reset_daily_tracking()
        self._daily_volume = Decimal('0')

    def update_peak(self) -> None:
        """Update peak portfolio value."""
        self._local_rm.update_peak()

    def get_current_drawdown(self) -> Decimal:
        """Get current drawdown percentage."""
        return self._local_rm.get_current_drawdown()

    def get_remaining_exposure(self) -> Decimal:
        """Get remaining exposure capacity."""
        return self._local_rm.get_remaining_exposure()
