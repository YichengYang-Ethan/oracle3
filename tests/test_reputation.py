"""Tests for Feature 5: Agent Reputation System."""

from __future__ import annotations

import pytest

from oracle3.onchain.reputation import ReputationManager, ReputationScore


class TestReputationScore:
    def test_frozen_dataclass(self):
        score = ReputationScore(
            wallet='abc123',
            win_rate=0.65,
            sharpe=1.5,
            total_trades=50,
            consistency=0.8,
            score=72.5,
        )
        assert score.wallet == 'abc123'
        assert score.win_rate == 0.65
        assert score.score == 72.5


class TestReputationManager:
    def test_init_no_logger(self):
        rm = ReputationManager()
        assert rm.wallet == ''
        assert rm._total_trades == 0

    def test_record_trade_result_win(self):
        rm = ReputationManager()
        rm.record_trade_result(0.05)
        assert rm._total_trades == 1
        assert rm._winning_trades == 1

    def test_record_trade_result_loss(self):
        rm = ReputationManager()
        rm.record_trade_result(-0.03)
        assert rm._total_trades == 1
        assert rm._winning_trades == 0

    def test_compute_score_no_trades(self):
        rm = ReputationManager()
        score = rm.compute_reputation_score()
        assert score.total_trades == 0
        assert score.win_rate == 0.0
        # Should still return a valid score
        assert 0 <= score.score <= 100

    def test_compute_score_with_trades(self):
        rm = ReputationManager()
        for _ in range(7):
            rm.record_trade_result(0.05)
        for _ in range(3):
            rm.record_trade_result(-0.02)

        score = rm.compute_reputation_score()
        assert score.total_trades == 10
        assert score.win_rate == 0.7
        assert score.score > 0

    def test_compute_score_explicit_params(self):
        rm = ReputationManager()
        score = rm.compute_reputation_score(
            wallet='explicit_wallet',
            win_rate=0.8,
            sharpe=2.0,
            total_trades=100,
            consistency=0.9,
        )
        assert score.wallet == 'explicit_wallet'
        assert score.win_rate == 0.8
        assert score.total_trades == 100
        assert score.score > 60  # high-performing agent

    def test_get_my_reputation(self):
        rm = ReputationManager()
        rm.record_trade_result(0.1)
        rep = rm.get_my_reputation()
        assert 'wallet' in rep
        assert 'score' in rep
        assert 'win_rate' in rep

    def test_get_agent_reputation_own_wallet(self):
        rm = ReputationManager()
        rm._wallet = 'my_wallet'
        rm.record_trade_result(0.05)
        rep = rm.get_agent_reputation('my_wallet')
        assert rep['total_trades'] == 1

    def test_get_agent_reputation_other_wallet(self):
        rm = ReputationManager()
        rep = rm.get_agent_reputation('other_wallet')
        assert rep['wallet'] == 'other_wallet'
        assert rep['total_trades'] == 0
        assert rep['score'] == 0.0

    @pytest.mark.asyncio
    async def test_maybe_write_summary_no_logger(self):
        rm = ReputationManager(write_interval=1)
        rm.record_trade_result(0.1)
        result = await rm.maybe_write_summary()
        assert result is None  # no logger configured

    @pytest.mark.asyncio
    async def test_maybe_write_summary_below_interval(self):
        rm = ReputationManager(write_interval=10)
        rm.record_trade_result(0.1)
        result = await rm.maybe_write_summary()
        assert result is None  # only 1 trade, need 10

    def test_consistency_high_for_uniform_returns(self):
        rm = ReputationManager()
        for _ in range(20):
            rm.record_trade_result(0.01)
        score = rm.compute_reputation_score()
        # All returns identical → std ≈ 0 → consistency ≈ 1
        assert score.consistency > 0.9

    def test_consistency_low_for_volatile_returns(self):
        rm = ReputationManager()
        for i in range(20):
            rm.record_trade_result(1.0 if i % 2 == 0 else -1.0)
        score = rm.compute_reputation_score()
        # Highly volatile → low consistency
        assert score.consistency < 0.1

    def test_sharpe_positive_for_varied_wins(self):
        rm = ReputationManager()
        for i in range(20):
            rm.record_trade_result(0.05 + i * 0.001)  # slight variation
        score = rm.compute_reputation_score()
        assert score.sharpe > 0

    def test_score_clamped_to_100(self):
        rm = ReputationManager()
        score = rm.compute_reputation_score(
            win_rate=1.0, sharpe=10.0, total_trades=1000, consistency=1.0,
        )
        assert score.score <= 100.0

    def test_score_clamped_to_0(self):
        rm = ReputationManager()
        score = rm.compute_reputation_score(
            win_rate=-1.0, sharpe=-10.0, total_trades=0, consistency=-1.0,
        )
        assert score.score >= 0.0
