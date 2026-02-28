"""Tests for alerts.alerter and alerts.telegram_alerter."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oracle3.alerts.alerter import Alerter, CompositeAlerter, LogAlerter
from oracle3.alerts.telegram_alerter import TelegramAlerter
from oracle3.ticker.ticker import PolyMarketTicker
from oracle3.trader.types import OrderFailureReason, Trade, TradeSide

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / 'trading_data'
    d.mkdir()
    return d


@pytest.fixture
def log_alerter(data_dir):
    return LogAlerter(data_dir)


@pytest.fixture
def poly_ticker():
    return PolyMarketTicker(symbol='SYM', name='Test market', token_id='tok1')


@pytest.fixture
def sample_trade(poly_ticker):
    return Trade(
        side=TradeSide.BUY,
        ticker=poly_ticker,
        price=Decimal('0.5'),
        quantity=Decimal('10'),
        commission=Decimal('0'),
    )


# ---------------------------------------------------------------------------
# LogAlerter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_alerter_send_writes_json_line(log_alerter, data_dir):
    await log_alerter.send('hello world', level='info')

    log_path = data_dir / 'alerts.log'
    assert log_path.exists()
    line = log_path.read_text().strip()
    entry = json.loads(line)
    assert entry['level'] == 'info'
    assert entry['message'] == 'hello world'
    assert 'timestamp' in entry


@pytest.mark.asyncio
async def test_log_alerter_multiple_sends_append(log_alerter, data_dir):
    await log_alerter.send('msg1', level='info')
    await log_alerter.send('msg2', level='warning')

    log_path = data_dir / 'alerts.log'
    lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]['message'] == 'msg1'
    assert lines[1]['level'] == 'warning'


# ---------------------------------------------------------------------------
# CompositeAlerter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_alerter_fans_out_to_all():
    a1 = AsyncMock(spec=Alerter)
    a2 = AsyncMock(spec=Alerter)
    composite = CompositeAlerter([a1, a2])

    await composite.send('test', level='info')

    a1.send.assert_awaited_once_with('test', 'info')
    a2.send.assert_awaited_once_with('test', 'info')


@pytest.mark.asyncio
async def test_composite_alerter_ignores_individual_failure():
    failing = AsyncMock(spec=Alerter)
    failing.send.side_effect = RuntimeError('boom')
    good = AsyncMock(spec=Alerter)

    composite = CompositeAlerter([failing, good])
    # Should not raise
    await composite.send('msg')

    good.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_composite_on_trade_fans_out(sample_trade):
    a1 = AsyncMock(spec=Alerter)
    a2 = AsyncMock(spec=Alerter)
    composite = CompositeAlerter([a1, a2])

    await composite.on_trade(sample_trade)

    a1.on_trade.assert_awaited_once_with(sample_trade)
    a2.on_trade.assert_awaited_once_with(sample_trade)


# ---------------------------------------------------------------------------
# TelegramAlerter — swallows network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_alerter_swallows_network_error():
    alerter = TelegramAlerter(bot_token='FAKE', chat_id='-1')

    with patch('oracle3.alerts.telegram_alerter.httpx.AsyncClient') as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception('network failure'))
        mock_client_cls.return_value = mock_client

        # Must NOT raise
        await alerter.send('test message', level='info')


@pytest.mark.asyncio
async def test_telegram_alerter_sends_correct_url():
    alerter = TelegramAlerter(bot_token='MYTOKEN', chat_id='12345')
    captured = {}

    async def fake_post(url, **kwargs):
        captured['url'] = url
        captured['json'] = kwargs.get('json', {})
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch('oracle3.alerts.telegram_alerter.httpx.AsyncClient') as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=fake_post)
        mock_client_cls.return_value = mock_client

        await alerter.send('hello', level='warning')

    assert 'MYTOKEN' in captured['url']
    assert captured['json']['chat_id'] == '12345'
    assert '⚠️' in captured['json']['text']
    assert 'hello' in captured['json']['text']


@pytest.mark.asyncio
async def test_telegram_alerter_swallows_http_error_response():
    alerter = TelegramAlerter(bot_token='FAKE', chat_id='-1')

    with patch('oracle3.alerts.telegram_alerter.httpx.AsyncClient') as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        response = MagicMock()
        response.raise_for_status.side_effect = Exception('http 500')
        mock_client.post = AsyncMock(return_value=response)
        mock_client_cls.return_value = mock_client

        # Must NOT raise
        await alerter.send('test message', level='error')


# ---------------------------------------------------------------------------
# Default on_* message formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_trade_formats_message(log_alerter, data_dir, sample_trade):
    await log_alerter.on_trade(sample_trade)
    log_path = data_dir / 'alerts.log'
    entry = json.loads(log_path.read_text().strip())
    assert 'BUY' in entry['message']
    assert '10' in entry['message']


@pytest.mark.asyncio
async def test_on_order_rejected_formats_message(log_alerter, data_dir, poly_ticker):
    await log_alerter.on_order_rejected(
        OrderFailureReason.RISK_CHECK_FAILED, poly_ticker
    )
    log_path = data_dir / 'alerts.log'
    entry = json.loads(log_path.read_text().strip())
    assert 'risk_check_failed' in entry['message']
    assert entry['level'] == 'warning'


@pytest.mark.asyncio
async def test_on_drawdown_alert_formats_message(log_alerter, data_dir):
    await log_alerter.on_drawdown_alert(Decimal('0.15'), Decimal('0.10'))
    log_path = data_dir / 'alerts.log'
    entry = json.loads(log_path.read_text().strip())
    assert '15.0%' in entry['message']
    assert '10.0%' in entry['message']
    assert entry['level'] == 'error'
