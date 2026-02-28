import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import litellm


def normalize_allocations(
    parsed: dict[str, Any],
) -> dict[str, float] | None:
    """Normalize allocation weights to sum to 1.0 with CASH always present.

    Clamps negative values to 0, caps individual weights at 1.0,
    and ensures CASH fills the remainder if not explicitly provided.
    """
    allocations = parsed.get('allocations', {}) if isinstance(parsed, dict) else {}
    if not isinstance(allocations, dict) or not allocations:
        return None

    cleaned: dict[str, float] = {}
    for key, value in allocations.items():
        if isinstance(value, (int, float)):
            weight = float(value)
            if weight < 0:
                weight = 0.0
            cleaned[key] = min(1.0, weight)

    if 'CASH' not in cleaned:
        non_cash_sum = sum(v for k, v in cleaned.items() if k != 'CASH')
        cleaned['CASH'] = max(0.0, 1.0 - non_cash_sum)

    total = sum(cleaned.values())
    if total <= 0:
        return {'CASH': 1.0}

    normalized = {k: (v / total) for k, v in cleaned.items()}

    non_cash_sum = sum(v for k, v in normalized.items() if k != 'CASH')
    normalized['CASH'] = max(0.0, 1.0 - non_cash_sum)

    return normalized


def parse_llm_response_to_json(content: str) -> dict[str, Any] | None:
    """Parse JSON from an LLM response, handling markdown fences and think tags."""
    try:
        # Remove <think>...</think> blocks
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)

        if '```json' in content:
            json_str = content.split('```json')[1].split('```')[0].strip()
        else:
            content = content.strip()
            start = content.find('{')
            end = content.rfind('}') + 1
            if start == -1 or end == 0:
                return None
            json_str = content[start:end]
        return json.loads(json_str)
    except (json.JSONDecodeError, IndexError):
        return None


class AllocationStrategy(ABC):
    """Strategy that produces target weight allocations via LLM analysis.

    This is a parallel interface to Strategy (which uses process_event).
    AllocationStrategy works with portfolio-level target weights instead
    of individual trade signals.
    """

    def __init__(
        self,
        name: str = 'allocation_agent',
        model: str = 'deepseek/deepseek-chat',
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ):
        self.name = name
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.logger = logging.getLogger(__name__)

    async def generate_allocation(
        self,
        market_data: dict[str, Any],
        account_data: dict[str, Any],
        news_data: dict[str, Any] | None = None,
    ) -> dict[str, float] | None:
        """Generate target weight allocations from market context.

        Returns a dict mapping asset identifiers to target weights (0-1),
        normalized to sum to 1.0, with CASH always present.
        Returns None on failure.
        """
        if not market_data:
            return None

        try:
            analysis = await self._prepare_market_analysis(market_data)
            prompt = await self._get_portfolio_prompt(
                analysis, market_data, account_data, news_data
            )

            messages = [{'role': 'user', 'content': prompt}]

            self.logger.info(f'Calling LLM ({self.model}) for allocation')

            response = await litellm.acompletion(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            content = response.choices[0].message.content
            self.logger.info(f'LLM response received for {self.name}')

            parsed = parse_llm_response_to_json(content)
            if not parsed:
                self.logger.error(f'Failed to parse LLM JSON: {content}')
                return None

            result = normalize_allocations(parsed)
            if result is None:
                self.logger.error(f'Failed to normalize allocations from: {parsed}')
            return result

        except Exception as e:
            self.logger.error(f'Allocation generation failed: {e}')
            return None

    @abstractmethod
    async def _prepare_market_analysis(
        self, market_data: dict[str, Any]
    ) -> str:
        """Prepare a text summary of current market conditions for the LLM prompt."""
        ...

    @abstractmethod
    async def _get_portfolio_prompt(
        self,
        analysis: str,
        market_data: dict[str, Any],
        account_data: dict[str, Any],
        news_data: dict[str, Any] | None = None,
    ) -> str:
        """Build the full LLM prompt requesting allocation weights.

        The prompt should instruct the LLM to respond with JSON containing:
        {
            "reasoning": "...",
            "allocations": {"ASSET1": 0.3, "ASSET2": 0.2, "CASH": 0.5}
        }
        """
        ...
