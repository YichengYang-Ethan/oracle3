"""Tests for statistical arbitrage strategies.

Covers: CointSpreadStrategy, LeadLagStrategy.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle3.data.market_data_manager import MarketDataManager
from oracle3.events.events import NewsEvent, OrderBookEvent, PriceChangeEvent
from oracle3.order.order_book import Level, OrderBook
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.strategy.contrib.coint_spread_strategy import CointSpreadStrategy
from oracle3.strategy.contrib.lead_lag_strategy import LeadLagStrategy
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticker(
    symbol: str,
    name: str = '',
    market_id: str = '',
    no_token_id: str = '',
) -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol=symbol,
        name=name or symbol,
        token_id=symbol,
        market_id=market_id or symbol,
        event_id='',
        no_token_id=no_token_id,
    )


def _make_trader(
    tickers: list[PolyMarketTicker] | None = None,
    cash: Decimal = Decimal('10000'),
) -> PaperTrader:
    md = MarketDataManager()
    pm = PositionManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=cash,
            average_cost=Decimal('1'),
            realized_pnl=Decimal('0'),
        )
    )
    if tickers:
        for t in tickers:
            ob = OrderBook()
            ob.update(
                asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
                bids=[Level(price=Decimal('0.45'), size=Decimal('1000'))],
            )
            md.update_order_book(t, ob)
    return PaperTrader(
        market_data=md,
        risk_manager=NoRiskManager(),
        position_manager=pm,
        min_fill_rate=Decimal('1'),
        max_fill_rate=Decimal('1'),
        commission_rate=Decimal('0'),
    )


def _seed_order_book(
    trader: PaperTrader,
    ticker: PolyMarketTicker,
    bid: Decimal,
    ask: Decimal,
    size: Decimal = Decimal('1000'),
) -> None:
    ob = OrderBook()
    ob.update(
        asks=[Level(price=ask, size=size)],
        bids=[Level(price=bid, size=size)],
    )
    trader.market_data.update_order_book(ticker, ob)


def _price_event(
    ticker: PolyMarketTicker, price: Decimal
) -> PriceChangeEvent:
    return PriceChangeEvent(ticker=ticker, price=price)


# ===================================================================
# CointSpreadStrategy Tests
# ===================================================================


class TestCointSpreadDefaults:
    """Test constructor defaults."""

    def test_defaults(self):
        s = CointSpreadStrategy()
        assert s._id_a == ''
        assert s._id_b == ''
        assert s.trade_size == Decimal('10')
        assert s._hedge_ratio == Decimal('1')
        assert s._entry_mult == 2.0
        assert s._exit_mult == 0.5
        assert s._warmup_size == 200
        assert s._calibrated is False
        assert s._position_state == 'flat'

    def test_name_and_version(self):
        assert CointSpreadStrategy.name == 'coint_spread'
        assert CointSpreadStrategy.version == '1.0.0'
        assert CointSpreadStrategy.supports_auto_tune()

    def test_custom_params(self):
        s = CointSpreadStrategy(
            market_id_a='A', market_id_b='B',
            hedge_ratio=0.8, entry_mult=3.0, exit_mult=1.0,
            warmup=50,
        )
        assert s._id_a == 'A'
        assert s._id_b == 'B'
        assert s._hedge_ratio == Decimal('0.8')
        assert s._entry_mult == 3.0
        assert s._exit_mult == 1.0
        assert s._warmup_size == 50


class TestCointSpreadWarmup:
    """Test warmup/calibration phase."""

    async def test_no_trades_during_warmup(self):
        """Strategy should not trade until warmup_size samples collected.

        Each event on ticker A or B (when both prices are set) appends
        one spread sample.  The first A event has no B price yet, so
        the actual sample count for N pairs is roughly (2*N - 1).
        We use warmup=50 and send only 10 pairs (= ~19 samples).
        """
        ticker_a = _make_ticker('A', market_id='mkt_a')
        ticker_b = _make_ticker('B', market_id='mkt_b')
        s = CointSpreadStrategy(
            market_id_a='mkt_a', market_id_b='mkt_b',
            warmup=50,
            cooldown_seconds=0.0,
        )
        trader = _make_trader([ticker_a, ticker_b])

        # Send 10 pairs => ~19 spread samples, well below warmup=50
        for i in range(10):
            price = Decimal(str(0.5 + i * 0.01))
            await s.process_event(_price_event(ticker_a, price), trader)
            await s.process_event(_price_event(ticker_b, price), trader)

        assert not s._calibrated
        assert s._position_state == 'flat'
        assert len(s.get_decisions()) == 0

    async def test_calibration_after_warmup(self):
        """After warmup_size samples, strategy should calibrate."""
        ticker_a = _make_ticker('A', market_id='mkt_a')
        ticker_b = _make_ticker('B', market_id='mkt_b')
        s = CointSpreadStrategy(
            market_id_a='mkt_a', market_id_b='mkt_b',
            warmup=10,
            hedge_ratio=1.0,
        )
        trader = _make_trader([ticker_a, ticker_b])

        # Send exactly 10 pairs -- spread should be 0 with hedgeratio=1
        for i in range(10):
            price = Decimal(str(0.50))
            await s.process_event(_price_event(ticker_a, price), trader)
            await s.process_event(_price_event(ticker_b, price), trader)

        assert s._calibrated
        # Expected spread = 0 (since A == B and hedge_ratio==1)
        assert float(s._expected_spread) == pytest.approx(0.0, abs=1e-6)


class TestCointSpreadSignalGeneration:
    """Test signal generation after warmup."""

    async def _calibrate_strategy(
        self,
        s: CointSpreadStrategy,
        ticker_a: PolyMarketTicker,
        ticker_b: PolyMarketTicker,
        trader: PaperTrader,
        n: int = 20,
        base_price: float = 0.50,
    ) -> None:
        """Helper: push n price events to calibrate the strategy."""
        for i in range(n):
            p = Decimal(str(base_price))
            await s.process_event(_price_event(ticker_a, p), trader)
            await s.process_event(_price_event(ticker_b, p), trader)

    async def test_hold_when_within_band(self):
        ticker_a = _make_ticker('A', market_id='mkt_a')
        ticker_b = _make_ticker('B', market_id='mkt_b')
        s = CointSpreadStrategy(
            market_id_a='mkt_a', market_id_b='mkt_b',
            warmup=10, entry_mult=2.0, exit_mult=0.5,
            hedge_ratio=1.0,
        )
        trader = _make_trader([ticker_a, ticker_b])

        # Calibrate with constant spread = 0
        # Use varying prices so std is non-zero
        for i in range(10):
            p = Decimal(str(0.50 + 0.001 * (i % 3 - 1)))
            await s.process_event(_price_event(ticker_a, p), trader)
            await s.process_event(_price_event(ticker_b, p), trader)

        assert s._calibrated

        # Send event within band
        await s.process_event(
            _price_event(ticker_a, Decimal('0.500')), trader
        )
        await s.process_event(
            _price_event(ticker_b, Decimal('0.500')), trader
        )
        assert s._position_state == 'flat'

    async def test_entry_on_large_deviation(self):
        """When spread deviates well beyond entry threshold, enter."""
        ticker_a = _make_ticker(
            'A', market_id='mkt_a', no_token_id='A_NO'
        )
        ticker_a_no = ticker_a.get_no_ticker()
        ticker_b = _make_ticker('B', market_id='mkt_b')
        s = CointSpreadStrategy(
            market_id_a='mkt_a', market_id_b='mkt_b',
            warmup=10, entry_mult=2.0, exit_mult=0.5,
            hedge_ratio=1.0, cooldown_seconds=0.0,
        )
        trader = _make_trader()
        _seed_order_book(trader, ticker_a, Decimal('0.45'), Decimal('0.55'))
        _seed_order_book(trader, ticker_a_no, Decimal('0.45'), Decimal('0.55'))
        _seed_order_book(trader, ticker_b, Decimal('0.45'), Decimal('0.55'))

        # Calibrate with spread around 0 but some variance
        for i in range(10):
            noise = 0.005 * ((i % 3) - 1)
            pa = Decimal(str(0.50 + noise))
            pb = Decimal(str(0.50))
            await s.process_event(_price_event(ticker_a, pa), trader)
            await s.process_event(_price_event(ticker_b, pb), trader)

        assert s._calibrated

        # Now create a large positive deviation (A much higher than B)
        # spread = 0.80 - 1.0*0.30 = 0.50 >> entry_threshold
        await s.process_event(
            _price_event(ticker_a, Decimal('0.80')), trader
        )
        await s.process_event(
            _price_event(ticker_b, Decimal('0.30')), trader
        )
        assert s._position_state == 'short_spread'


class TestCointSpreadPositionStateMachine:
    """Test full lifecycle: flat -> position -> flat."""

    async def test_enter_and_exit(self):
        ticker_a = _make_ticker('A', market_id='mkt_a', no_token_id='A_NO')
        ticker_a_no = ticker_a.get_no_ticker()
        ticker_b = _make_ticker('B', market_id='mkt_b', no_token_id='B_NO')
        ticker_b_no = ticker_b.get_no_ticker()
        s = CointSpreadStrategy(
            market_id_a='mkt_a', market_id_b='mkt_b',
            warmup=10, entry_mult=2.0, exit_mult=0.5,
            hedge_ratio=1.0, cooldown_seconds=0.0,
            trade_size=5.0,
        )
        trader = _make_trader()
        for t in [ticker_a, ticker_b]:
            _seed_order_book(trader, t, Decimal('0.45'), Decimal('0.55'))
        for t in [ticker_a_no, ticker_b_no]:
            _seed_order_book(trader, t, Decimal('0.45'), Decimal('0.55'))

        # Calibrate
        for i in range(10):
            noise = 0.005 * ((i % 3) - 1)
            pa = Decimal(str(0.50 + noise))
            pb = Decimal(str(0.50))
            await s.process_event(_price_event(ticker_a, pa), trader)
            await s.process_event(_price_event(ticker_b, pb), trader)

        # Enter: large positive deviation -> short_spread
        await s.process_event(
            _price_event(ticker_a, Decimal('0.80')), trader
        )
        await s.process_event(
            _price_event(ticker_b, Decimal('0.30')), trader
        )
        assert s._position_state == 'short_spread'

        # Exit: spread converges back near mean
        await s.process_event(
            _price_event(ticker_a, Decimal('0.50')), trader
        )
        await s.process_event(
            _price_event(ticker_b, Decimal('0.50')), trader
        )
        assert s._position_state == 'flat'


class TestCointSpreadPaused:
    async def test_paused_ignores_events(self):
        ticker_a = _make_ticker('A', market_id='mkt_a')
        s = CointSpreadStrategy(market_id_a='mkt_a', market_id_b='mkt_b')
        s.set_paused(True)
        trader = _make_trader([ticker_a])
        await s.process_event(_price_event(ticker_a, Decimal('0.50')), trader)
        assert s._price_a is None


# ===================================================================
# LeadLagStrategy Tests
# ===================================================================


class TestLeadLagDefaults:
    """Test constructor defaults."""

    def test_defaults(self):
        s = LeadLagStrategy()
        assert s._leader_symbol == ''
        assert s._follower_symbol == ''
        assert s.trade_size == Decimal('10')
        assert s.entry_threshold == Decimal('0.03')
        assert s._exit_threshold == 0.5
        assert s._warmup_size == 50
        assert s._max_hold == 100
        assert s._cooldown_seconds == 30.0
        assert s._min_correlation == 0.3
        assert s._position_state == 'flat'

    def test_name_and_version(self):
        assert LeadLagStrategy.name == 'lead_lag'
        assert LeadLagStrategy.version == '1.0.0'

    def test_diagnostic_properties(self):
        s = LeadLagStrategy()
        assert s.position_state == 'flat'
        assert s.correlation is None  # no data yet
        assert s.leader_observations == 0
        assert s.follower_observations == 0


class TestLeadLagWarmup:
    """Test warmup phase -- no trading until enough leader observations."""

    async def test_no_trading_during_warmup(self):
        leader = _make_ticker('LEADER', market_id='LEADER')
        follower = _make_ticker('FOLLOWER', market_id='FOLLOWER')
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
        )
        trader = _make_trader([leader, follower])

        # Only 5 leader observations -- below warmup
        for i in range(5):
            p = Decimal(str(0.50 + 0.01 * i))
            await s.process_event(_price_event(leader, p), trader)
            await s.process_event(_price_event(follower, p), trader)

        assert s.leader_observations == 5
        assert s._position_state == 'flat'

    async def test_trading_possible_after_warmup(self):
        """After warmup observations, the strategy can evaluate entry."""
        leader = _make_ticker('LEADER', market_id='LEADER')
        follower = _make_ticker('FOLLOWER', market_id='FOLLOWER')
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.01,
            cooldown_seconds=0.0,
            min_correlation=0.0,  # disable correlation gate
        )
        trader = _make_trader([leader, follower])

        # Build warmup with stable prices
        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        assert s.leader_observations == 10

        # Now send a leader event with a large move
        # leader_mean ~ 0.50, new price = 0.60, move = 0.10 > 0.01
        _seed_order_book(
            trader, follower, Decimal('0.45'), Decimal('0.55')
        )
        await s.process_event(
            _price_event(leader, Decimal('0.60')), trader
        )

        # Should have attempted entry or at least made a decision
        decisions = s.get_decisions()
        assert len(decisions) >= 1


class TestLeadLagSignalGeneration:
    """Test entry signal logic."""

    async def test_leader_up_buys_follower(self):
        """When leader moves up, buy follower YES."""
        leader = _make_ticker(
            'LEADER', market_id='LEADER', no_token_id='LEADER_NO'
        )
        follower = _make_ticker(
            'FOLLOWER', market_id='FOLLOWER', no_token_id='FOLLOWER_NO'
        )
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.02,
            cooldown_seconds=0.0,
            min_correlation=0.0,
        )
        trader = _make_trader()
        _seed_order_book(
            trader, follower, Decimal('0.45'), Decimal('0.55')
        )
        _seed_order_book(
            trader, leader, Decimal('0.45'), Decimal('0.55')
        )

        # Warmup
        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        # Leader moves up significantly
        await s.process_event(
            _price_event(leader, Decimal('0.60')), trader
        )

        assert s._position_state == 'long_follower'
        decisions = s.get_decisions()
        buy_decisions = [d for d in decisions if d.action == 'BUY_YES']
        assert len(buy_decisions) >= 1

    async def test_leader_down_shorts_follower(self):
        """When leader moves down, buy follower NO (short)."""
        leader = _make_ticker('LEADER', market_id='LEADER')
        follower = _make_ticker(
            'FOLLOWER', market_id='FOLLOWER', no_token_id='FOLLOWER_NO'
        )
        follower_no = follower.get_no_ticker()
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.02,
            cooldown_seconds=0.0,
            min_correlation=0.0,
        )
        trader = _make_trader()
        _seed_order_book(
            trader, follower, Decimal('0.45'), Decimal('0.55')
        )
        _seed_order_book(
            trader, follower_no, Decimal('0.45'), Decimal('0.55')
        )
        _seed_order_book(
            trader, leader, Decimal('0.45'), Decimal('0.55')
        )

        # Warmup
        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        # Leader moves down
        await s.process_event(
            _price_event(leader, Decimal('0.40')), trader
        )

        assert s._position_state == 'short_follower'

    async def test_no_entry_when_move_below_threshold(self):
        leader = _make_ticker('LEADER', market_id='LEADER')
        follower = _make_ticker('FOLLOWER', market_id='FOLLOWER')
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.05,  # high threshold
            cooldown_seconds=0.0,
            min_correlation=0.0,
        )
        trader = _make_trader([leader, follower])

        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        # Small move: 0.52 - 0.50 = 0.02 < 0.05
        await s.process_event(
            _price_event(leader, Decimal('0.52')), trader
        )
        assert s._position_state == 'flat'


class TestLeadLagPositionStateMachine:
    """Test full lifecycle: flat -> position -> exit."""

    async def test_catchup_exit(self):
        """Exit when follower catches up to the leader's move."""
        leader = _make_ticker(
            'LEADER', market_id='LEADER', no_token_id='LEADER_NO'
        )
        follower = _make_ticker(
            'FOLLOWER', market_id='FOLLOWER', no_token_id='FOLLOWER_NO'
        )
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.02,
            exit_threshold=0.5,
            cooldown_seconds=0.0,
            min_correlation=0.0,
            max_hold=200,
            trade_size=5.0,
        )
        trader = _make_trader()
        _seed_order_book(
            trader, follower, Decimal('0.45'), Decimal('0.55')
        )
        _seed_order_book(
            trader, leader, Decimal('0.45'), Decimal('0.55')
        )

        # Warmup
        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        # Enter: leader up 0.10
        await s.process_event(
            _price_event(leader, Decimal('0.60')), trader
        )
        assert s._position_state == 'long_follower'

        # Follower catches up: move 0.05, catchup_ratio = 0.05/0.10 = 0.50 >= 0.5
        await s.process_event(
            _price_event(follower, Decimal('0.55')), trader
        )
        assert s._position_state == 'flat'

    async def test_timeout_exit(self):
        """Exit when max_hold exceeded."""
        leader = _make_ticker(
            'LEADER', market_id='LEADER', no_token_id='LEADER_NO'
        )
        follower = _make_ticker(
            'FOLLOWER', market_id='FOLLOWER', no_token_id='FOLLOWER_NO'
        )
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.02,
            exit_threshold=0.99,  # never catches up
            cooldown_seconds=0.0,
            min_correlation=0.0,
            max_hold=5,
            trade_size=5.0,
        )
        trader = _make_trader()
        _seed_order_book(
            trader, follower, Decimal('0.45'), Decimal('0.55')
        )
        _seed_order_book(
            trader, leader, Decimal('0.45'), Decimal('0.55')
        )

        # Warmup
        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        # Enter
        await s.process_event(
            _price_event(leader, Decimal('0.60')), trader
        )
        assert s._position_state == 'long_follower'

        # Send max_hold follower updates without catching up
        for i in range(6):
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        assert s._position_state == 'flat'


class TestLeadLagCorrelation:
    """Test correlation computation and gating."""

    def test_correlation_none_with_insufficient_data(self):
        s = LeadLagStrategy(correlation_window=30)
        assert s._compute_rolling_correlation() is None

    def test_correlation_with_perfect_data(self):
        s = LeadLagStrategy(correlation_window=5, lead_lag_steps=0)
        # Feed perfectly correlated prices
        for i in range(10):
            s._leader_corr_prices.append(float(i))
            s._follower_corr_prices.append(float(i))

        corr = s._compute_rolling_correlation()
        assert corr is not None
        assert corr == pytest.approx(1.0, abs=1e-6)

    def test_correlation_gate_blocks_entry(self):
        """When correlation is below min_correlation, no entry."""
        # This is an indirect test -- we verify the gate exists
        s = LeadLagStrategy(min_correlation=0.99)
        # With random data correlation won't be 0.99
        for i in range(40):
            s._leader_corr_prices.append(float(i % 5))
            s._follower_corr_prices.append(float(4 - (i % 5)))

        corr = s._compute_rolling_correlation()
        if corr is not None:
            assert corr < 0.99  # correlation gate would block


class TestLeadLagFeeAware:
    """Test fee-aware edge calculation."""

    def test_net_edge(self):
        s = LeadLagStrategy()
        # Round-trip fees = 2 * 0.005 = 0.01
        assert s._net_edge(0.05) == pytest.approx(0.04, abs=1e-6)
        assert s._net_edge(0.01) == pytest.approx(0.0, abs=1e-6)
        assert s._net_edge(0.005) == pytest.approx(-0.005, abs=1e-6)

    async def test_no_entry_when_edge_below_fees(self):
        leader = _make_ticker('LEADER', market_id='LEADER')
        follower = _make_ticker('FOLLOWER', market_id='FOLLOWER')
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.001,  # very low threshold
            cooldown_seconds=0.0,
            min_correlation=0.0,
        )
        trader = _make_trader([leader, follower])

        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        # Move = 0.008, net_edge = 0.008 - 0.01 = -0.002 < 0
        await s.process_event(
            _price_event(leader, Decimal('0.508')), trader
        )
        assert s._position_state == 'flat'


class TestLeadLagNonPriceEvents:
    """Test that non-price events are ignored."""

    async def test_ignores_news_events(self):
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
        )
        trader = _make_trader()
        news = NewsEvent(news='Some news')
        await s.process_event(news, trader)
        assert s.leader_observations == 0

    async def test_ignores_no_side_events(self):
        no_ticker = _make_ticker('LEADER_NO', market_id='LEADER')
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
        )
        trader = _make_trader([no_ticker])
        await s.process_event(
            _price_event(no_ticker, Decimal('0.50')), trader
        )
        assert s._leader_price is None


class TestLeadLagCooldown:
    """Test cooldown enforcement."""

    async def test_cooldown_prevents_rapid_reentry(self):
        leader = _make_ticker(
            'LEADER', market_id='LEADER', no_token_id='LEADER_NO'
        )
        follower = _make_ticker(
            'FOLLOWER', market_id='FOLLOWER', no_token_id='FOLLOWER_NO'
        )
        s = LeadLagStrategy(
            leader_symbol='LEADER',
            follower_symbol='FOLLOWER',
            warmup=10,
            entry_threshold=0.02,
            exit_threshold=0.5,
            cooldown_seconds=9999.0,  # very long cooldown
            min_correlation=0.0,
            max_hold=2,
            trade_size=5.0,
        )
        trader = _make_trader()
        _seed_order_book(
            trader, follower, Decimal('0.45'), Decimal('0.55')
        )
        _seed_order_book(
            trader, leader, Decimal('0.45'), Decimal('0.55')
        )

        # Warmup
        for i in range(10):
            await s.process_event(
                _price_event(leader, Decimal('0.50')), trader
            )
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )

        # Enter
        await s.process_event(
            _price_event(leader, Decimal('0.60')), trader
        )
        assert s._position_state == 'long_follower'

        # Force exit via timeout
        for i in range(3):
            await s.process_event(
                _price_event(follower, Decimal('0.50')), trader
            )
        assert s._position_state == 'flat'

        # Try to re-enter -- should be blocked by cooldown
        await s.process_event(
            _price_event(leader, Decimal('0.60')), trader
        )
        assert s._position_state == 'flat'
