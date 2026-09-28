#!/usr/bin/env python3
# mypy: ignore-errors
"""Oracle3 end-to-end paper-trading demo of the on-chain feature set.

This is a plumbing demo, not a performance test. It replays recorded DFlow
order-book data, reads the live Solana slot and wallet balance, and exercises
each on-chain component once. The Polymarket counterpart prices (a synthetic
3-8% spread), the whale signals and the reputation P&L series are simulated,
so the arbitrage counts and scores it prints say nothing about real edge.

Data sources:
- Recorded DFlow order-book data (parquet, 4,534 events, 884 active tickers)
- Solana mainnet RPC (live slot check and wallet balance query)

Steps:
 0. Environment check: Solana RPC connectivity and local data load
 1. Feature 1: cross-market arbitrage detection (CrossMarketArbitrageStrategy)
 2. Feature 2: on-chain risk checks (OnChainRiskManager)
 3. Feature 3: on-chain data signals (OnChainSignalSource)
 4. Feature 4: MEV protection (JitoSubmitter)
 5. Feature 5: agent reputation (ReputationManager)
 6. Feature 6: multi-agent coordination (AgentCoordinator)
 7. Feature 7: flash-loan arbitrage (experimental FlashLoanArbitrage; disabled, returns not-implemented)
 8. Feature 8: atomic multi-leg trades (AtomicTrader)
 9. PaperTrader replay on the recorded data (50+ orders)
10. Arbitrage strategy process_event end to end
11. Final reputation summary
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RPC_URL = 'https://api.mainnet-beta.solana.com'
WALLET_ADDRESS = '7RQ3YL4cLNbQbwAUHBP6GzdRbG6NRng8qBcHbiDrf8Ae'
PARQUET_PATH = Path('data/episodes/dflow_15min/dflow_events.parquet')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASS = 0
_FAIL = 0


def section(title: str) -> None:
    print(f'\n{"=" * 72}')
    print(f'  {title}')
    print(f'{"=" * 72}')


def ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    print(f'  [OK] {msg}')


def info(msg: str) -> None:
    print(f'  [..] {msg}')


def fail(msg: str) -> None:
    global _FAIL
    _FAIL += 1
    print(f'  [FAIL] {msg}')


# ---------------------------------------------------------------------------
# Live RPC calls
# ---------------------------------------------------------------------------


async def fetch_solana_slot() -> int:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            RPC_URL,
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'getSlot',
            },
        )
        return resp.json()['result']


async def fetch_sol_balance(address: str) -> float:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            RPC_URL,
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'getBalance',
                'params': [address],
            },
        )
        lamports = resp.json()['result']['value']
        return lamports / 1e9


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


async def run_simulation() -> None:  # noqa: C901
    print('\n' + '=' * 72)
    print('  Oracle3 end-to-end paper-trading demo')
    print(f'  Wallet: {WALLET_ADDRESS}')
    print('  Mode: paper trading (no real funds submitted)')
    print('  Data: recorded DFlow order books + Solana mainnet RPC')
    print('=' * 72)

    # ==================================================================
    # Step 0: environment check
    # ==================================================================
    section('Step 0: environment and data check')

    # Solana RPC
    slot = await fetch_solana_slot()
    ok(f'Solana RPC reachable, slot: {slot}')

    sol_balance = await fetch_sol_balance(WALLET_ADDRESS)
    ok(f'Wallet balance: {sol_balance:.6f} SOL')

    # Load the recorded local data
    df = pd.read_parquet(PARQUET_PATH)
    ok(
        f'Loaded recorded DFlow data: {len(df)} events, {df["ticker"].nunique()} tickers'
    )

    # Keep active tickers (prices that moved)
    ticker_stats = df.groupby('ticker').agg(
        min_p=('price', 'min'),
        max_p=('price', 'max'),
        cnt=('price', 'count'),
        mean_p=('price', 'mean'),
    )
    varied = ticker_stats[ticker_stats['min_p'] != ticker_stats['max_p']]
    active_tickers = varied.sort_values('cnt', ascending=False).head(30)
    ok(
        f'Active tickers (prices moved): {len(varied)}, using the top {len(active_tickers)}'
    )

    for t, row in active_tickers.head(5).iterrows():
        print(f'    {str(t):<45} [{row.min_p:.4f}, {row.max_p:.4f}]  n={int(row.cnt)}')

    # ==================================================================
    # Step 1: Feature 1, cross-market arbitrage detection
    # ==================================================================
    section('Step 1: Feature 1, cross-market arbitrage detection')

    from oracle3.strategy.contrib.cross_market_arbitrage_strategy import (
        CrossMarketArbitrageStrategy,
    )
    from oracle3.ticker.ticker import PolyMarketTicker, SolanaTicker

    arb_strategy = CrossMarketArbitrageStrategy(
        min_edge=0.02,
        trade_size=50.0,
        fee_rate=0.01,
        cooldown_seconds=5.0,
    )

    # Build SolanaTicker objects from the recorded DFlow data
    solana_tickers: list[SolanaTicker] = []
    for ticker_sym in active_tickers.index:
        row = active_tickers.loc[ticker_sym]
        st = SolanaTicker(
            symbol=str(ticker_sym),
            name=str(ticker_sym).replace('-', ' '),
            market_ticker=str(ticker_sym),
            event_ticker=str(ticker_sym).split('-')[0],
        )
        solana_tickers.append(st)
        arb_strategy.register_price('dflow', st, Decimal(str(round(row.mean_p, 4))))

    ok(f'Registered {len(solana_tickers)} DFlow SolanaTickers (recorded mean prices)')

    # Simulated Polymarket side: reuse the event names and add a synthetic 3-8% spread
    poly_tickers: list[PolyMarketTicker] = []
    for i, (ticker_sym, row) in enumerate(active_tickers.iterrows()):
        # Similar names so that SequenceMatcher pairs them
        name = str(ticker_sym).replace('-', ' ')
        pt = PolyMarketTicker(
            symbol=f'POLY_{str(ticker_sym)[:20]}',
            name=name,
            token_id=f'poly_tok_{i}',
            market_id=f'poly_mkt_{i}',
            event_id=f'poly_evt_{i}',
        )
        poly_tickers.append(pt)
        offset = 0.03 + (i % 6) * 0.01
        poly_price = Decimal(str(round(row.mean_p + offset, 4)))
        arb_strategy.register_price('polymarket', pt, poly_price)

    ok(
        f'Registered {len(poly_tickers)} simulated Polymarket tickers (synthetic 3-8% spread)'
    )

    opportunities = arb_strategy.find_arbitrage_opportunities()
    ok(f'Detected {len(opportunities)} opportunities (on synthetic spreads)')

    for opp in opportunities[:5]:
        print(f'    {opp["label"][:50]}')
        print(f'      DFlow @ {opp["price_a"]:.4f}  vs  Poly @ {opp["price_b"]:.4f}')
        print(
            f'      spread={opp["spread"]:.4f}  profit=${opp["expected_profit"]:.2f}  fees=${opp["fees"]:.2f}'
        )

    # ==================================================================
    # Step 2: Feature 2, on-chain risk checks
    # ==================================================================
    section('Step 2: Feature 2, on-chain risk checks')

    from oracle3.data.market_data_manager import MarketDataManager
    from oracle3.position.position_manager import PositionManager
    from oracle3.risk.onchain_risk_manager import OnChainRiskManager
    from oracle3.trader.types import TradeSide

    md = MarketDataManager()
    pm = PositionManager()
    onchain_risk = OnChainRiskManager(
        position_manager=pm,
        market_data=md,
        rpc_url=RPC_URL,
        max_single_trade_size=Decimal('500'),
        max_position_size=Decimal('2000'),
        max_total_exposure=Decimal('10000'),
        daily_loss_limit=Decimal('1000'),
        enable_simulation=True,
    )

    t0 = solana_tickers[0]

    # Within limits
    allowed = await onchain_risk.check_trade(
        t0, TradeSide.BUY, Decimal('50'), Decimal('0.45')
    )
    ok(f'Within-limit check (50 @ 0.45): {"allowed" if allowed else "rejected"}')

    # Over the limit
    blocked = await onchain_risk.check_trade(
        t0, TradeSide.BUY, Decimal('600'), Decimal('0.45')
    )
    ok(
        f'Over-limit check (600 @ 0.45): {"rejected" if not blocked else "unexpectedly allowed"}'
    )

    # Agent tool
    risk_status = onchain_risk.get_risk_status()
    ok(
        f'Risk state: daily_used={risk_status["daily_volume_used"]}, '
        f'remaining={risk_status["daily_remaining"]}'
    )
    print(
        f'    Limits: max_trade={risk_status["max_single_trade"]}, '
        f'max_pos={risk_status["max_position_size"]}, '
        f'exposure={risk_status["max_total_exposure"]}'
    )

    # ==================================================================
    # Step 3: Feature 3, on-chain data signals
    # ==================================================================
    section('Step 3: Feature 3, on-chain data signals')

    from oracle3.data.live.onchain_signal_source import (
        OnChainSignal,
        OnChainSignalSource,
        WatchedWallet,
    )

    signal_source = OnChainSignalSource(
        rpc_url=RPC_URL,
        watched_wallets=[
            WatchedWallet(address=WALLET_ADDRESS, label='oracle3-agent'),
            WatchedWallet(
                address='9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM',
                label='dflow-treasury',
            ),
        ],
        polling_interval=60.0,
        large_transfer_threshold=1000.0,
    )

    info('Scanning on-chain signals (live RPC)...')
    try:
        await signal_source._poll_wallet_balances()
        signals = signal_source.get_onchain_signals(limit=5)
        ok(f'On-chain scan finished: {len(signals)} signals')
        for sig in signals:
            print(
                f'    {sig["signal_type"]}: wallet={sig.get("wallet","")[:16]}.. '
                f'amount={sig.get("amount",0):.2f} {sig.get("token","")} '
                f'dir={sig.get("direction","")}'
            )
    except Exception as e:
        info(f'Partial RPC scan failure: {type(e).__name__}: {str(e)[:60]}')

    # Inject simulated whale signals (real wallet addresses)
    whale_signals = [
        OnChainSignal(
            signal_type='whale_transfer',
            wallet=WALLET_ADDRESS,
            amount=50000.0,
            direction='outflow',
            token='SOL',
            timestamp=time.time(),
            label='oracle3-agent large SOL outflow',
        ),
        OnChainSignal(
            signal_type='large_transfer',
            wallet='9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM',
            amount=120000.0,
            direction='inflow',
            token='USDC',
            timestamp=time.time(),
            label='dflow-treasury USDC deposit',
        ),
        OnChainSignal(
            signal_type='tvl_change',
            wallet='DFlow Protocol',
            amount=2_500_000.0,
            direction='increase',
            token='TVL',
            timestamp=time.time(),
            label='DFlow TVL +2.5M',
        ),
    ]
    for ws in whale_signals:
        signal_source._signals.append(ws)
    ok(f'Injected {len(whale_signals)} simulated on-chain signals')

    all_signals = signal_source.get_onchain_signals(limit=10)
    for sig in all_signals:
        print(
            f'    [{sig["signal_type"]}] {sig.get("label","")[:50]} '
            f'({sig.get("amount",0):,.0f} {sig.get("token","")})'
        )

    # ==================================================================
    # Step 4: Feature 4, MEV protection
    # ==================================================================
    section('Step 4: Feature 4, MEV protection (Jito)')

    from oracle3.trader.jito_submitter import JitoSubmitter

    mock_kp = MagicMock()
    mock_kp.pubkey.return_value = MagicMock(__str__=lambda s: WALLET_ADDRESS)

    jito = JitoSubmitter(
        keypair=mock_kp,
        rpc_url=RPC_URL,
        tip_lamports=10_000,
    )

    mev = jito.get_mev_protection_status()
    ok(
        f'Jito MEV protection: enabled={mev["enabled"]}, tip={mev["tip_lamports"]} lamports'
    )
    print(
        f'    total_submitted={mev["total_submitted"]}, fallbacks={mev["fallback_count"]}'
    )
    print(f'    jito_url={mev["jito_url"][:40]}...')

    # ==================================================================
    # Step 5: Feature 5, agent reputation
    # ==================================================================
    section('Step 5: Feature 5, agent reputation score')

    from oracle3.onchain.reputation import ReputationManager

    rep_mgr = ReputationManager(write_interval=5)
    rep_mgr._wallet = WALLET_ADDRESS

    # 30 simulated trade results (a fixed mixed win/loss series, not real P&L)
    simulated_pnl = [
        0.05,
        0.03,
        -0.01,
        0.08,
        -0.02,
        0.04,
        0.06,
        -0.03,
        0.02,
        0.07,
        -0.01,
        0.05,
        0.04,
        -0.02,
        0.03,
        0.06,
        -0.04,
        0.08,
        0.01,
        0.05,
        0.03,
        -0.01,
        0.09,
        -0.03,
        0.04,
        0.02,
        0.07,
        -0.02,
        0.06,
        0.05,
    ]
    for pnl in simulated_pnl:
        rep_mgr.record_trade_result(pnl)

    ok(f'Recorded {len(simulated_pnl)} simulated P&L values')

    my_rep = rep_mgr.get_my_reputation()
    ok(f'Reputation score: {my_rep["score"]:.1f}/100')
    print(
        f'    win_rate={my_rep["win_rate"]:.1%}, Sharpe={my_rep["sharpe"]:.3f}, '
        f'consistency={my_rep["consistency"]:.3f}, trades={my_rep["total_trades"]}'
    )

    other_rep = rep_mgr.get_agent_reputation('unknown_agent_xyz')
    ok(
        f'Unknown agent lookup: score={other_rep["score"]}, trades={other_rep["total_trades"]}'
    )

    # ==================================================================
    # Step 6: Feature 6, multi-agent coordination
    # ==================================================================
    section('Step 6: Feature 6, multi-agent pipeline')

    from oracle3.agent.coordinator import (
        AgentCoordinator,
        ExecutionAgent,
        RiskAgent,
        SignalAgent,
    )

    coordinator = AgentCoordinator(
        signal_agent=SignalAgent(),
        risk_agent=RiskAgent(),
        execution_agent=ExecutionAgent(),
    )

    # Build a pipeline task from the arbitrage detection output
    if opportunities:
        best = opportunities[0]
        task = {
            'type': 'arbitrage_execution',
            'market_a': best['market_a'],
            'market_b': best['market_b'],
            'spread': best['spread'],
            'expected_profit': best['expected_profit'],
            'trade_size': 50.0,
            'risk_check': True,
        }
    else:
        task = {
            'type': 'market_analysis',
            'ticker': solana_tickers[0].symbol,
            'action': 'evaluate',
        }

    info(f'Starting pipeline: {task["type"]}')
    pipeline_result = await coordinator.run_pipeline(task)
    ok(f'Pipeline finished: success={pipeline_result.success}')
    print(f'    ticker={pipeline_result.ticker}, side={pipeline_result.side}')
    print(f'    qty={pipeline_result.quantity}, price={pipeline_result.price}')
    if pipeline_result.error:
        print(f'    error={pipeline_result.error[:80]}')

    # delegate_to_specialist
    for agent_type, task_desc in [
        (
            'signal',
            'Analyze whale SOL outflow correlation with DFlow prediction markets',
        ),
        (
            'risk',
            'Evaluate portfolio exposure after 20 long positions on sports events',
        ),
        ('execution', 'Execute hedged buy on KXATPCHALLENGERMATCH-26MAR01SCHPIR-SCH'),
    ]:
        result = await coordinator.delegate_to_specialist(agent_type, task_desc)
        ok(f'[{agent_type}] delegated: {result[:60]}{"..." if len(result)>60 else ""}')

    # ==================================================================
    # Step 7: Feature 7, flash-loan arbitrage (experimental)
    # ==================================================================
    section('Step 7: Feature 7, flash-loan arbitrage (experimental, disabled)')

    from oracle3.experimental.flash_loan import FlashLoanArbitrage

    flash_loan = FlashLoanArbitrage(
        keypair=None,
        rpc_url=RPC_URL,
        protocol='marginfi',
        max_borrow=10_000.0,
        min_profit_bps=50,
    )

    mkt_a = solana_tickers[0].symbol
    mkt_b = solana_tickers[1].symbol

    # Over the borrow limit
    over = await flash_loan.execute_flash_arbitrage(mkt_a, mkt_b, 20_000.0)
    ok(
        f'Over limit (20k > 10k max): success={over["success"]}  error={over["error"][:40]}'
    )

    # Within the limit (the prototype is disabled and returns not-implemented)
    normal = await flash_loan.execute_flash_arbitrage(mkt_a, mkt_b, 5_000.0)
    ok(f'Within limit (5k): success={normal["success"]}, protocol={normal["protocol"]}')

    # Several amounts
    for amount in [1_000, 3_000, 8_000]:
        r = await flash_loan.execute_flash_arbitrage(mkt_a, mkt_b, float(amount))
        info(f'  {amount:,} USDC: success={r["success"]}, protocol={r["protocol"]}')

    stats = flash_loan.stats
    ok(
        f'Flash-loan stats: attempts={stats["total_attempts"]}, '
        f'successes={stats["successes"]}, profit={stats["total_profit"]}'
    )

    # ==================================================================
    # Step 8: Feature 8, atomic multi-leg trades
    # ==================================================================
    section('Step 8: Feature 8, atomic multi-leg trades')

    from oracle3.trader.atomic_trader import AtomicTrader

    atomic = AtomicTrader(keypair=None, rpc_url=RPC_URL)

    # Prediction market leg + Jupiter hedge
    jup_result = await atomic.place_hedged_order(
        prediction_market_symbol=solana_tickers[0].symbol,
        prediction_side='buy',
        prediction_qty=100.0,
        prediction_price=0.28,
        hedge_instrument='jupiter_swap',
        hedge_ticker='SOL/USDC',
        hedge_side='sell',
        hedge_qty=2.0,
        hedge_price=180.0,
    )
    ok(
        f'Jupiter hedge: success={jup_result["success"]}, legs={len(jup_result["legs"])}'
    )
    for leg in jup_result['legs']:
        print(
            f'    {leg["instrument_type"]}: {leg["side"]} {leg["qty"]} {leg["ticker"][:30]} @ {leg["price"]}'
        )
    print(f'    Total cost: ${jup_result["total_cost"]}')

    # Prediction market leg + Drift perpetual hedge
    drift_result = await atomic.place_hedged_order(
        prediction_market_symbol=solana_tickers[2].symbol
        if len(solana_tickers) > 2
        else 'ETH_5K',
        prediction_side='buy',
        prediction_qty=200.0,
        prediction_price=0.15,
        hedge_instrument='drift_perp',
        hedge_ticker='SOL-PERP',
        hedge_side='sell',
        hedge_qty=5.0,
        hedge_price=175.0,
    )
    ok(
        f'Drift perpetual hedge: success={drift_result["success"]}, cost=${drift_result["total_cost"]}'
    )

    # Several atomic trades
    for i in range(3):
        t = solana_tickers[3 + i] if len(solana_tickers) > 3 + i else solana_tickers[0]
        r = await atomic.place_hedged_order(
            prediction_market_symbol=t.symbol,
            prediction_side='buy' if i % 2 == 0 else 'sell',
            prediction_qty=50.0 + i * 25,
            prediction_price=0.35 + i * 0.05,
            hedge_instrument='jupiter_swap',
            hedge_ticker='SOL/USDC',
            hedge_side='sell' if i % 2 == 0 else 'buy',
            hedge_qty=1.0 + i * 0.5,
            hedge_price=170.0 + i * 5,
        )
        info(f'  Atomic trade #{i+3}: {t.symbol[:30]}, cost=${r["total_cost"]}')

    a_stats = atomic.stats
    ok(
        f'Atomic trade stats: attempts={a_stats["total_attempts"]}, successes={a_stats["successes"]}'
    )

    # ==================================================================
    # Step 9: PaperTrader replay on recorded data
    # ==================================================================
    section('Step 9: PaperTrader replay on recorded data')

    from oracle3.events.events import PriceChangeEvent
    from oracle3.position.position_manager import Position
    from oracle3.risk.risk_manager import StandardRiskManager
    from oracle3.ticker.ticker import CashTicker
    from oracle3.trader.paper_trader import PaperTrader

    sim_md = MarketDataManager()
    sim_pm = PositionManager()

    # Seed $10,000 of paper USDC
    sim_pm.update_position(
        Position(
            ticker=CashTicker.DFLOW_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('1'),
            realized_pnl=Decimal('0'),
        )
    )
    ok('Seeded paper balance: $10,000 USDC')

    sim_risk = StandardRiskManager(
        position_manager=sim_pm,
        market_data=sim_md,
        max_single_trade_size=Decimal('500'),
        max_position_size=Decimal('2000'),
        max_total_exposure=Decimal('10000'),
        initial_capital=Decimal('10000'),
    )
    paper = PaperTrader(
        market_data=sim_md,
        risk_manager=sim_risk,
        position_manager=sim_pm,
        min_fill_rate=Decimal('0.95'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.001'),
    )

    # Replay the recorded parquet data
    trade_log: list[dict] = []
    filled_count = 0
    rejected_count = 0

    # Sort by time and take the first 100 events with price changes
    replay_df = df[df['ticker'].isin(active_tickers.index)].sort_values('ts').head(100)
    ok(f'Replaying {len(replay_df)} recorded events')

    prev_prices: dict[str, float] = {}

    for _idx, (_, event_row) in enumerate(replay_df.iterrows()):
        ticker_sym = str(event_row['ticker'])
        price = float(event_row['price'])
        _ = str(event_row.get('side', 'bid'))

        ticker = SolanaTicker(
            symbol=ticker_sym,
            name=ticker_sym.replace('-', ' '),
            market_ticker=ticker_sym,
            event_ticker=ticker_sym.split('-')[0],
        )

        # Feed market data
        sim_md.process_price_change_event(
            PriceChangeEvent(
                ticker=ticker,
                price=Decimal(str(price)),
            )
        )

        # Toy rule: trade on price changes
        prev = prev_prices.get(ticker_sym)
        prev_prices[ticker_sym] = price

        if prev is None:
            continue  # First observation, skip

        if price == prev:
            continue  # No price change, skip

        # Price up -> buy; price down -> sell
        if price > prev:
            trade_side = TradeSide.BUY
            limit = Decimal(str(round(price + 0.01, 4)))
            qty = Decimal('20')
        else:
            trade_side = TradeSide.SELL
            limit = Decimal(str(round(price - 0.01, 4)))
            qty = Decimal('15')

        result = await paper.place_order(
            side=trade_side,
            ticker=ticker,
            limit_price=limit,
            quantity=qty,
        )

        status = (
            'FILLED'
            if not result.failure_reason
            else f'REJ({str(result.failure_reason)[:20]})'
        )
        if not result.failure_reason:
            filled_count += 1
            # Record in the reputation system
            pnl = (
                float(price - prev) * float(qty)
                if trade_side == TradeSide.BUY
                else float(prev - price) * float(qty)
            )
            rep_mgr.record_trade_result(pnl)
        else:
            rejected_count += 1

        trade_log.append(
            {
                'ticker': ticker_sym[:30],
                'side': trade_side.value,
                'qty': float(qty),
                'price': float(limit),
                'status': status,
            }
        )

        if len(trade_log) <= 10 or len(trade_log) % 10 == 0:
            print(
                f'    #{len(trade_log):>3} {trade_side.value:4s} {qty:>5} x '
                f'{ticker_sym[:28]:<28} @ {limit:<8} → {status}'
            )

    ok(
        f'Replay finished: {len(trade_log)} orders, {filled_count} filled, {rejected_count} rejected'
    )

    # Portfolio
    portfolio = sim_pm.get_portfolio_value(sim_md)
    print(f'    Portfolio value: {portfolio}')

    # ==================================================================
    # Step 10: arbitrage strategy process_event end to end
    # ==================================================================
    section('Step 10: arbitrage strategy process_event end to end')

    t_arb = solana_tickers[0]
    arb_event = PriceChangeEvent(
        ticker=t_arb,
        price=Decimal(str(round(active_tickers.iloc[0].mean_p, 4))),
    )
    arb_strategy.bind_context(arb_event, paper)
    await arb_strategy.process_event(arb_event, paper)
    ok(
        f'process_event finished, current opportunities: {len(arb_strategy.opportunities)}'
    )

    # A sequence of price events
    for i in range(min(5, len(solana_tickers))):
        t = solana_tickers[i]
        row = active_tickers.iloc[i]
        # Simulated price moves
        for delta in [0.01, -0.02, 0.03]:
            ev = PriceChangeEvent(
                ticker=t,
                price=Decimal(str(round(row.mean_p + delta, 4))),
            )
            await arb_strategy.process_event(ev, paper)

    ok(
        f'Event sequence finished, final opportunities: {len(arb_strategy.opportunities)}'
    )

    # ==================================================================
    # Step 11: final reputation summary
    # ==================================================================
    section('Step 11: final reputation summary')

    final = rep_mgr.get_my_reputation()
    ok(f'Final reputation score: {final["score"]:.1f}/100')
    print(f'    Trades: {final["total_trades"]}')
    print(f'    Win rate: {final["win_rate"]:.1%}')
    print(f'    Sharpe: {final["sharpe"]:.3f}')
    print(f'    Consistency: {final["consistency"]:.3f}')
    print(f'    Wallet: {final["wallet"][:20]}...')

    # ==================================================================
    # Summary
    # ==================================================================
    section('Demo finished: component check summary')

    features = [
        (
            'Feature 1: cross-market arbitrage detection',
            len(opportunities) > 0,
            f'{len(opportunities)} opportunities',
        ),
        (
            'Feature 2: on-chain risk checks',
            risk_status is not None,
            f'daily_used={risk_status["daily_volume_used"]}',
        ),
        (
            'Feature 3: on-chain data signals',
            len(all_signals) > 0,
            f'{len(all_signals)} signals',
        ),
        (
            'Feature 4: MEV protection (Jito)',
            mev['enabled'],
            f'tip={mev["tip_lamports"]}',
        ),
        (
            'Feature 5: agent reputation',
            final['score'] > 0,
            f'score={final["score"]:.1f}',
        ),
        (
            'Feature 6: multi-agent coordination',
            True,
            f'pipeline ran, success={pipeline_result.success}',
        ),
        (
            'Feature 7: flash-loan arbitrage (experimental)',
            stats['total_attempts'] > 0,
            f'{stats["total_attempts"]} attempts',
        ),
        (
            'Feature 8: atomic multi-leg trades',
            a_stats['total_attempts'] > 0,
            f'{a_stats["total_attempts"]} attempts',
        ),
    ]

    passed = sum(1 for _, s, _ in features if s)
    for name, status, detail in features:
        icon = 'PASS' if status else 'FAIL'
        print(f'  [{icon}] {name}  ({detail})')

    print(f'\n  {"=" * 50}')
    print(f'  Result: {passed}/{len(features)} components ran')
    print(f'  Paper orders: {len(trade_log)} ({filled_count} filled)')
    print(f'  Agent reputation: {final["score"]:.1f}/100')
    print(f'  Opportunities (synthetic spreads): {len(opportunities)}')
    print(f'  Solana slot: {slot}')
    print(f'  Wallet SOL: {sol_balance:.6f}')
    print(f'  {"=" * 50}')
    print()


if __name__ == '__main__':
    asyncio.run(run_simulation())
