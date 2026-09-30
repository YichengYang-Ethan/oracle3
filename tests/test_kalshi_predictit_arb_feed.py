"""Tests for examples/strategies/kalshi_predictit_arb_feed.py (no network)."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from examples.strategies import kalshi_predictit_arb_feed as feed
from examples.strategies.kalshi_predictit_arb_feed import (
    BASE_USDC,
    ArbFeedSignalStrategy,
    ArbScanner,
    KalshiPredictItArbDataSource,
    PaymentFailed,
    PaymentRefused,
)
from oracle3.events.events import NewsEvent

PAY_TO = '0x' + '11' * 20
BODY = {
    'opportunities': [
        {
            'pair': 'A <-> B',
            'best_direction': {'net_yield_c': 2.5, 'net_yield_pct': 2.7},
            'executable': True,
        }
    ]
}


def _requirement(**kwargs: object) -> dict:
    requirement = {
        'scheme': 'exact',
        'network': 'eip155:8453',
        'amount': '20000',
        'asset': BASE_USDC,
        'payTo': PAY_TO,
        'maxTimeoutSeconds': 60,
        'extra': {'name': 'USD Coin', 'version': '2'},
    }
    requirement.update(kwargs)
    return requirement


def _b64url_decode(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))


def _payment_required(
    accepts: list, in_header: bool = True, urlsafe: bool = False
) -> httpx.Response:
    required = {
        'x402Version': 2,
        'resource': {'url': feed.DEFAULT_URL},
        'accepts': accepts,
    }
    if in_header:
        raw = json.dumps(required).encode()
        if urlsafe:
            header = base64.urlsafe_b64encode(raw).decode().rstrip('=')
        else:
            header = base64.b64encode(raw).decode()
        return httpx.Response(402, headers={'PAYMENT-REQUIRED': header})
    return httpx.Response(402, json=required)


class Recorder:
    """httpx.MockTransport handler replaying canned responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('X402_WALLET_KEY', raising=False)


@pytest.fixture
def account():
    eth_account = pytest.importorskip('eth_account')
    return eth_account.Account.create()  # throwaway, never funded


def _live(account, recorder: Recorder) -> ArbScanner:
    return ArbScanner(private_key=account.key, transport=httpx.MockTransport(recorder))


# -- demo / fixture ---------------------------------------------------------


def _offline_scanner() -> ArbScanner:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError('demo mode must not make requests')

    return ArbScanner(transport=httpx.MockTransport(fail))


def test_demo_mode_uses_fixture(no_env_key: None) -> None:
    scanner = _offline_scanner()
    assert scanner.demo
    response = scanner.scan()
    assert response['sample_data'] is True
    assert len(response['opportunities']) == 2
    assert all(o['executable'] for o in response['opportunities'])


def test_demo_mode_filters(no_env_key: None) -> None:
    scanner = _offline_scanner()
    assert len(scanner.scan(mode='all', limit=25)['opportunities']) == 5
    assert len(scanner.scan(mode='all', limit=1)['opportunities']) == 1
    assert len(scanner.scan(q='senate dem', mode='all')['opportunities']) == 1


def test_scan_validation(no_env_key: None) -> None:
    scanner = _offline_scanner()
    with pytest.raises(ValueError):
        scanner.scan(limit=0)
    with pytest.raises(ValueError):
        scanner.scan(mode='foo')


def test_default_cap_is_two_cents(account) -> None:
    assert ArbScanner(private_key=account.key).max_atomic == 20000


def test_env_key_enables_live(monkeypatch: pytest.MonkeyPatch, account) -> None:
    monkeypatch.setenv('X402_WALLET_KEY', account.key.hex())
    assert not ArbScanner().demo


# -- 402 -> pay -> retry ----------------------------------------------------


def test_no_payment_required(account) -> None:
    recorder = Recorder(httpx.Response(200, json=BODY))
    assert _live(account, recorder).scan(q='x') == BODY
    assert len(recorder.requests) == 1
    assert recorder.requests[0].url.params['q'] == 'x'


@pytest.mark.parametrize(
    ('in_header', 'urlsafe'),
    [(True, False), (True, True), (False, False)],
    ids=['header', 'header_urlsafe', 'body'],
)
def test_pay_and_retry(account, in_header: bool, urlsafe: bool) -> None:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    requirement = _requirement()
    settle = base64.b64encode(json.dumps({'success': True}).encode()).decode()
    recorder = Recorder(
        _payment_required([requirement], in_header, urlsafe),
        httpx.Response(200, json=BODY, headers={'PAYMENT-RESPONSE': settle}),
    )
    scanner = _live(account, recorder)
    now = int(time.time())
    assert scanner.scan(q='senate', limit=5) == BODY
    assert scanner.last_payment_response == {'success': True}
    assert len(recorder.requests) == 2

    headers = recorder.requests[1].headers
    assert headers['PAYMENT-SIGNATURE'] == headers['X-PAYMENT']
    # outgoing header is base64url without padding
    assert not set('+/=') & set(headers['PAYMENT-SIGNATURE'])
    payload = _b64url_decode(headers['PAYMENT-SIGNATURE'])
    assert payload['x402Version'] == 2
    assert payload['accepted'] == requirement
    assert payload['resource'] == {'url': feed.DEFAULT_URL}
    auth = payload['payload']['authorization']
    assert auth['from'] == account.address
    assert auth['to'] == PAY_TO
    assert auth['value'] == '20000'
    assert all(isinstance(v, str) for v in auth.values())
    assert abs(int(auth['validAfter']) - (now - 600)) <= 5
    assert abs(int(auth['validBefore']) - (now + 60)) <= 5
    assert len(bytes.fromhex(auth['nonce'][2:])) == 32

    message = dict(auth)
    for key in ('value', 'validAfter', 'validBefore'):
        message[key] = int(message[key])
    message['nonce'] = bytes.fromhex(message['nonce'][2:])
    signable = encode_typed_data(
        full_message=feed.build_typed_data(requirement, message)
    )
    signature = payload['payload']['signature']
    assert Account.recover_message(signable, signature=signature) == account.address


def test_payment_failed(account) -> None:
    recorder = Recorder(
        _payment_required([_requirement()]), httpx.Response(402, json={})
    )
    with pytest.raises(PaymentFailed):
        _live(account, recorder).scan()


def test_typed_data_matches_manual_eip712_hash(account) -> None:
    from eth_abi import encode
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    auth = {
        'from': account.address,
        'to': PAY_TO,
        'value': 20000,
        'validAfter': 1,
        'validBefore': 2,
        'nonce': b'\x07' * 32,
    }
    domain_type = keccak(
        text='EIP712Domain(string name,string version,uint256 chainId,'
        'address verifyingContract)'
    )
    domain = keccak(
        encode(
            ['bytes32', 'bytes32', 'bytes32', 'uint256', 'address'],
            [domain_type, keccak(text='USD Coin'), keccak(text='2'), 8453, BASE_USDC],
        )
    )
    auth_type = keccak(
        text='TransferWithAuthorization(address from,address to,uint256 value,'
        'uint256 validAfter,uint256 validBefore,bytes32 nonce)'
    )
    struct = keccak(
        encode(
            [
                'bytes32',
                'address',
                'address',
                'uint256',
                'uint256',
                'uint256',
                'bytes32',
            ],
            [auth_type, auth['from'], PAY_TO, 20000, 1, 2, auth['nonce']],
        )
    )
    manual = Account.unsafe_sign_hash(
        keccak(b'\x19\x01' + domain + struct), account.key
    )
    typed = account.sign_message(
        encode_typed_data(full_message=feed.build_typed_data(_requirement(), auth))
    )
    assert manual.signature == typed.signature


# -- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    'accepts',
    [
        [_requirement(network='eip155:1')],
        [_requirement(asset='0x' + '22' * 20)],
        [_requirement(amount='20001')],
        [_requirement(amount='0')],
        [_requirement(scheme='upto')],
        [_requirement(payTo='merchant')],
        [],
    ],
    ids=['network', 'asset', 'over_cap', 'zero', 'scheme', 'pay_to', 'empty'],
)
def test_refusals(account, accepts: list) -> None:
    recorder = Recorder(_payment_required(accepts))
    with pytest.raises(PaymentRefused):
        _live(account, recorder).scan()
    assert len(recorder.requests) == 1


def test_refuses_402_without_requirements(account) -> None:
    recorder = Recorder(httpx.Response(402, text='nope'))
    with pytest.raises(PaymentRefused):
        _live(account, recorder).scan()


def test_select_first_acceptable() -> None:
    good = _requirement()
    assert (
        feed.select_requirement([_requirement(network='base-sepolia'), good], 20000)
        == good
    )
    v1 = _requirement(network='base', amount=None, maxAmountRequired='10000')
    del v1['amount']
    assert feed.select_requirement([v1], 20000) == v1


def test_b64_decode_accepts_standard_and_urlsafe() -> None:
    data = {'k': '???>>>' * 3, 'n': 1}
    raw = json.dumps(data).encode()
    std = base64.b64encode(raw).decode()
    url = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    assert set('+/') & set(std) and set('-_') & set(url)
    assert feed._b64_json(std) == data
    assert feed._b64_json(url) == data
    assert feed._b64_json('not base64!') is None


# -- data source and strategy -----------------------------------------------


def test_normalise_response_tolerates_garbage() -> None:
    assert feed.normalise_response(None) == {'opportunities': []}
    assert feed.net_yield({'best_direction': None}) is None
    assert feed.net_yield({'best_direction': {'net_yield_c': 'x'}}) is None


async def test_data_source_emits_news_events(no_env_key: None) -> None:
    source = KalshiPredictItArbDataSource(
        scanner=_offline_scanner(), mode='all', max_polls=1
    )
    await source.start()
    events = []
    while (event := await source.get_next_event()) is not None:
        events.append(event)
    await source.stop()
    assert len(events) == 5
    assert all(isinstance(e, NewsEvent) and e.source == feed.SOURCE for e in events)
    assert all(e.title.startswith('[SAMPLE DATA]') for e in events)
    # unchanged pairs are not re-emitted
    assert await source.poll_once() == []


async def test_data_source_survives_errors(no_env_key: None) -> None:
    class Broken:
        def scan(self, **kwargs: object) -> dict:
            raise PaymentRefused('no')

    source = KalshiPredictItArbDataSource(scanner=Broken(), max_polls=1)  # type: ignore[arg-type]
    await source.start()
    assert await source.get_next_event() is None
    await source.stop()


async def test_strategy_records_signals(no_env_key: None) -> None:
    source = KalshiPredictItArbDataSource(scanner=_offline_scanner(), mode='all')
    events = await source.poll_once()
    strategy = ArbFeedSignalStrategy(min_net_yield_c=1.0)
    for event in events:
        await strategy.process_event(event, trader=None)  # type: ignore[arg-type]
    await strategy.process_event(NewsEvent(news='other'), trader=None)  # type: ignore[arg-type]
    actions = [d.action for d in strategy.get_decisions()]
    assert actions == ['ARB_SIGNAL', 'ARB_SIGNAL', 'HOLD', 'HOLD', 'HOLD']
    assert all(not d.executed for d in strategy.get_decisions())
