"""Generate shareable Solana Blink URLs for prediction market trades."""

from __future__ import annotations

from urllib.parse import quote, urlencode


def generate_blink_url(
    base_url: str,
    market_ticker: str,
    side: str = 'yes',
    amount: int | None = None,
) -> str:
    """Generate a shareable Blink URL for a prediction market trade.

    Args:
        base_url: The base URL of the Blinks server (e.g., 'https://oracle3.example.com')
        market_ticker: The DFlow market ticker
        side: 'yes' or 'no'
        amount: Optional pre-filled amount

    Returns:
        A shareable Solana Action URL
    """
    base = base_url.rstrip('/')
    action_url = f'{base}/api/trade/{quote(market_ticker)}'

    if amount is not None:
        action_url += f'/execute?{urlencode({"side": side, "amount": amount})}'

    # Wrap in solana-action: protocol for wallet auto-detection
    return f'solana-action:{action_url}'


def generate_action_url(
    base_url: str,
    market_ticker: str,
) -> str:
    """Generate the raw Action GET URL (for embedding in tweets, etc.)."""
    base = base_url.rstrip('/')
    return f'{base}/api/trade/{quote(market_ticker)}'
