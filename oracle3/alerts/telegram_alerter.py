"""Telegram alerter — sends notifications via the Telegram Bot API.

Uses ``httpx.AsyncClient`` (already a project dependency).  Network errors
are **always swallowed** so a Telegram outage can never crash the engine.
"""

from __future__ import annotations

import logging

import httpx

from oracle3.alerts.alerter import Alerter

logger = logging.getLogger(__name__)

_LEVEL_EMOJI = {
    'info': 'ℹ️',
    'warning': '⚠️',
    'error': '🚨',
}


class TelegramAlerter(Alerter):
    """Send alerts to a Telegram chat via the Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._api_url = f'https://api.telegram.org/bot{bot_token}/sendMessage'

    async def send(self, message: str, level: str = 'info') -> None:
        emoji = _LEVEL_EMOJI.get(level, 'ℹ️')
        text = f'{emoji} {message}'
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._api_url,
                    json={'chat_id': self.chat_id, 'text': text},
                    timeout=5.0,
                )
                response.raise_for_status()
        except Exception:
            # Alerts must never crash the trading engine.
            logger.debug('TelegramAlerter: failed to send message', exc_info=True)
