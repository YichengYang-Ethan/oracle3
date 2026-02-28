"""Prompt templates and mock LLM provider for news-based prediction market analysis."""

from __future__ import annotations

import random
import textwrap


def build_system_prompt() -> str:
    """Return a system prompt establishing the LLM as a prediction market analyst."""
    return textwrap.dedent("""\
        You are an expert prediction market trader and analyst. You specialise in
        evaluating news events and their impact on binary-outcome prediction markets.

        Your approach is grounded in rational, Bayesian thinking:
        - Start from the current market price as a base probability.
        - Update your estimate only when new evidence genuinely shifts the likelihood.
        - Distinguish between noise and signal in the news cycle.
        - Account for the possibility that information is already priced in.

        You MUST respond with a single JSON object and nothing else. No markdown, no
        commentary outside the JSON.

        Expected JSON schema:
        {
            "action": "buy" | "sell" | "hold",
            "confidence": <float 0.0-1.0>,
            "reasoning": "<brief explanation>",
            "target_price": <float 0.0-1.0>
        }

        Field definitions:
        - action: "buy" to go long YES, "sell" to reduce/exit, "hold" to do nothing.
        - confidence: how strongly the news shifts the probability (0 = no shift, 1 = decisive).
        - reasoning: one or two sentences explaining your logic.
        - target_price: your updated fair-value probability for the YES outcome.""")


def format_news_for_prompt(news_items: list[dict], max_items: int = 5) -> str:
    """Format news items into a clean string for inclusion in prompts.

    Items are sorted most-recent-first and truncated to *max_items*.  Each dict
    is expected to contain the keys ``title``, ``source``, ``snippet``, and
    ``published_at``.
    """
    if not news_items:
        return "(No recent news available.)"

    # Sort by published_at descending; tolerate missing key gracefully.
    sorted_items = sorted(
        news_items,
        key=lambda n: n.get("published_at", ""),
        reverse=True,
    )[:max_items]

    lines: list[str] = []
    for idx, item in enumerate(sorted_items, start=1):
        title = item.get("title", "Untitled")
        source = item.get("source", "Unknown")
        snippet = item.get("snippet", "")
        published = item.get("published_at", "N/A")
        lines.append(
            f"  [{idx}] {title}\n"
            f"      Source: {source} | Published: {published}\n"
            f"      {snippet}"
        )

    header = f"Recent news ({len(sorted_items)} of {len(news_items)} items):"
    return header + "\n" + "\n\n".join(lines)


def build_news_analysis_prompt(
    market_name: str,
    ticker_symbol: str,
    news_items: list[dict],
    current_price: float,
    price_change_pct: float,
    price_trend: str,
    current_position_qty: float,
    current_position_avg_cost: float,
    available_cash: float,
) -> str:
    """Build the main analysis prompt for prediction market trading.

    Parameters
    ----------
    market_name:
        The market question / name (e.g. "Will X happen by Y?").
    ticker_symbol:
        The ticker symbol for this contract.
    news_items:
        List of dicts with keys: title, source, snippet, published_at.
    current_price:
        Current YES probability (0-1).
    price_change_pct:
        Recent price change expressed as a percentage.
    price_trend:
        One of "rising", "falling", or "stable".
    current_position_qty:
        Current position quantity (0 if none).
    current_position_avg_cost:
        Average cost basis of the current position.
    available_cash:
        Cash available for new trades.
    """
    news_block = format_news_for_prompt(news_items)

    position_desc = "No current position."
    if current_position_qty != 0:
        position_desc = (
            f"Holding {current_position_qty:.4f} contracts at avg cost "
            f"${current_position_avg_cost:.4f}."
        )

    return textwrap.dedent(f"""\
        === PREDICTION MARKET ANALYSIS REQUEST ===

        This is a binary prediction market. Prices represent probabilities between 0
        and 1, where 1 means the market believes the event will certainly happen (YES)
        and 0 means it certainly will not (NO). Trading works by buying YES contracts
        when you believe the true probability is higher than the market price, or
        selling when you believe it is lower.

        --- Market ---
        Question : {market_name}
        Ticker   : {ticker_symbol}

        --- Price Context ---
        Current price  : {current_price:.4f}  (i.e. market implies {current_price * 100:.1f}% chance of YES)
        Recent change  : {price_change_pct:+.2f}%
        Trend          : {price_trend}

        --- Position Context ---
        {position_desc}
        Available cash : ${available_cash:.2f}

        --- Recent News ---
        {news_block}

        === INSTRUCTIONS ===

        Analyse the news above in the context of this prediction market. Consider:
        1. Does any of this news materially change the probability of the event?
        2. Is this information likely already priced in by other traders?
        3. How does the current trend align with or contradict the news?

        Your confidence value should reflect how strongly the news shifts the
        fundamental probability — not merely whether news exists. A confidence of 0
        means the news is irrelevant or already priced in; a confidence near 1 means
        the news is decisive and not yet reflected in the price.

        Respond with a single JSON object:
        {{"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, "reasoning": "...", "target_price": 0.0-1.0}}""")


class MockLLMProvider:
    """A deterministic (seedable) mock LLM provider for testing without API keys."""

    def __init__(
        self,
        default_action: str = "hold",
        default_confidence: float = 0.3,
        seed: int | None = None,
    ) -> None:
        self.default_action = default_action
        self.default_confidence = default_confidence
        self._rng = random.Random(seed)

    async def generate_response(
        self,
        news_items: list[dict],
        current_price: float,
        price_change_pct: float,
    ) -> dict:
        """Generate a mock LLM analysis response.

        Logic
        -----
        - If *price_change_pct* > 5 %  -> action = ``"sell"``, confidence based on magnitude.
        - If *price_change_pct* < -5 % -> action = ``"buy"``,  confidence based on magnitude.
        - If ``len(news_items) >= 3``  -> slight confidence bump (more information).
        - Otherwise                     -> hold with low confidence.
        - A small random jitter is applied via the seeded RNG.
        """
        jitter = self._rng.uniform(-0.05, 0.05)

        if price_change_pct > 5.0:
            action = "sell"
            confidence = min(1.0, 0.4 + abs(price_change_pct) / 100.0 + jitter)
            reasoning = (
                f"Price surged {price_change_pct:+.1f}% — may be overextended. "
                "Considering taking profits or fading the move."
            )
            target_price = max(0.0, min(1.0, current_price - 0.03 + jitter))
        elif price_change_pct < -5.0:
            action = "buy"
            confidence = min(1.0, 0.4 + abs(price_change_pct) / 100.0 + jitter)
            reasoning = (
                f"Price dropped {price_change_pct:+.1f}% — potential overreaction. "
                "Looking for a mean-reversion opportunity."
            )
            target_price = max(0.0, min(1.0, current_price + 0.03 + jitter))
        else:
            action = self.default_action
            confidence = max(0.0, min(1.0, self.default_confidence + jitter))
            reasoning = "No significant price movement to act on."
            target_price = max(0.0, min(1.0, current_price + jitter))

        # Bump confidence when there is a richer news set.
        if len(news_items) >= 3:
            confidence = min(1.0, confidence + 0.1)

        # Clamp final values.
        confidence = round(max(0.0, min(1.0, confidence)), 4)
        target_price = round(max(0.0, min(1.0, target_price)), 4)

        return {
            "action": action,
            "confidence": confidence,
            "reasoning": reasoning,
            "target_price": target_price,
        }
