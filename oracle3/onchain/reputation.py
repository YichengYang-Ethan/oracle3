"""Agent reputation system — on-chain scoring via Solana Memo program.

Computes a 0–100 reputation score based on trading performance:
  win_rate (30%) + sharpe (30%) + trade_count (20%) + consistency (20%)

Agent tools:
  get_my_reputation() -> dict
  get_agent_reputation(wallet) -> dict
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReputationScore:
    """Computed reputation for an agent wallet."""

    wallet: str
    win_rate: float
    sharpe: float
    total_trades: int
    consistency: float  # 0-1, stddev of returns normalised
    score: float  # 0-100 composite


class ReputationManager:
    """Manages on-chain reputation scoring and memo writing.

    Wraps an ``OnChainLogger`` to write periodic reputation summaries
    as Solana Memo transactions.
    """

    def __init__(
        self,
        on_chain_logger: Any | None = None,
        write_interval: int = 10,
    ) -> None:
        """
        Args:
            on_chain_logger: An ``OnChainLogger`` instance for writing memos.
            write_interval: Write a reputation memo every N trades.
        """
        self._logger = on_chain_logger
        self._write_interval = write_interval

        # Running stats
        self._trade_returns: list[float] = []
        self._winning_trades: int = 0
        self._total_trades: int = 0
        self._trade_count_since_write: int = 0
        self._wallet: str = ''

        if on_chain_logger is not None:
            self._wallet = getattr(on_chain_logger, 'public_key', '')

    @property
    def wallet(self) -> str:
        return self._wallet

    def record_trade_result(self, pnl: float) -> None:
        """Record the PnL result of a completed trade.

        Args:
            pnl: The profit/loss from the trade.
        """
        self._trade_returns.append(pnl)
        self._total_trades += 1
        if pnl > 0:
            self._winning_trades += 1
        self._trade_count_since_write += 1

    def compute_reputation_score(
        self,
        wallet: str | None = None,
        win_rate: float | None = None,
        sharpe: float | None = None,
        total_trades: int | None = None,
        consistency: float | None = None,
    ) -> ReputationScore:
        """Compute a weighted reputation score.

        If parameters are not provided, uses internal running stats.

        Weights:
            win_rate: 30%
            sharpe: 30%
            trade_count: 20%
            consistency: 20%

        Returns:
            ReputationScore with a score 0-100.
        """
        w = wallet or self._wallet

        # Win rate (0-1)
        wr = win_rate if win_rate is not None else self._compute_win_rate()
        # Sharpe ratio (normalise to 0-1 via sigmoid-like mapping)
        sr = sharpe if sharpe is not None else self._compute_sharpe()
        # Total trades
        tc = total_trades if total_trades is not None else self._total_trades
        # Consistency (0-1, where 1 = perfectly consistent)
        cs = consistency if consistency is not None else self._compute_consistency()

        # Normalize components to 0-1
        wr_norm = max(0.0, min(1.0, wr))
        sr_norm = max(0.0, min(1.0, (sr + 2.0) / 5.0))  # map [-2, 3] -> [0, 1]
        tc_norm = min(1.0, tc / 100.0)  # 100+ trades = full score
        cs_norm = max(0.0, min(1.0, cs))

        # Weighted composite
        score = (
            wr_norm * 30.0
            + sr_norm * 30.0
            + tc_norm * 20.0
            + cs_norm * 20.0
        )
        score = round(max(0.0, min(100.0, score)), 2)

        return ReputationScore(
            wallet=w,
            win_rate=round(wr, 4),
            sharpe=round(sr, 4),
            total_trades=tc,
            consistency=round(cs, 4),
            score=score,
        )

    def get_my_reputation(self) -> dict[str, Any]:
        """Agent tool: get the current agent's reputation score."""
        rep = self.compute_reputation_score()
        return asdict(rep)

    def get_agent_reputation(self, wallet: str) -> dict[str, Any]:
        """Agent tool: get reputation score for a specific wallet.

        Note: Without on-chain history, returns a placeholder with
        the wallet address. In production, this would read memo history
        from the Solana blockchain.
        """
        # If it's our own wallet, use local stats
        if wallet == self._wallet:
            return self.get_my_reputation()

        # For other wallets, return a default score
        # In production, this would fetch memo history via RPC
        return asdict(
            ReputationScore(
                wallet=wallet,
                win_rate=0.0,
                sharpe=0.0,
                total_trades=0,
                consistency=0.0,
                score=0.0,
            )
        )

    async def maybe_write_summary(self) -> str | None:
        """Write a reputation memo if the write interval has been reached.

        Returns:
            The memo transaction signature, or None if no write occurred.
        """
        if self._trade_count_since_write < self._write_interval:
            return None
        if self._logger is None:
            return None

        self._trade_count_since_write = 0
        rep = self.compute_reputation_score()

        memo_data = json.dumps(
            {
                'app': 'oracle3',
                'action': 'reputation',
                'wallet': rep.wallet[:12],
                'score': rep.score,
                'wr': rep.win_rate,
                'sharpe': rep.sharpe,
                'trades': rep.total_trades,
                'consistency': rep.consistency,
            },
            separators=(',', ':'),
        )

        try:
            sig = await self._logger.log_trade(
                market_ticker='__reputation__',
                side='summary',
                price=rep.score,
                quantity=rep.total_trades,
                trade_signature=memo_data[:64],
            )
            logger.info('Reputation memo written: score=%.1f, sig=%s', rep.score, sig[:16])
            return sig
        except Exception:
            logger.debug('Failed to write reputation memo', exc_info=True)
            return None

    # ---- Internal stat computation ----

    def _compute_win_rate(self) -> float:
        if self._total_trades == 0:
            return 0.0
        return self._winning_trades / self._total_trades

    def _compute_sharpe(self) -> float:
        if len(self._trade_returns) < 2:
            return 0.0
        mean = sum(self._trade_returns) / len(self._trade_returns)
        variance = sum(
            (r - mean) ** 2 for r in self._trade_returns
        ) / len(self._trade_returns)
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std == 0:
            return 0.0
        return mean / std

    def _compute_consistency(self) -> float:
        """Consistency = 1 - normalised stddev of returns."""
        if len(self._trade_returns) < 2:
            return 0.5  # neutral for insufficient data
        mean = sum(self._trade_returns) / len(self._trade_returns)
        variance = sum(
            (r - mean) ** 2 for r in self._trade_returns
        ) / len(self._trade_returns)
        std = math.sqrt(variance) if variance > 0 else 0.0
        # Normalize: stddev of 0.5 or more -> consistency 0
        return max(0.0, 1.0 - std / 0.5)
