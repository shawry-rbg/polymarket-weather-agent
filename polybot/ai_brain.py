"""
AI Brain - Intelligent trade analysis engine for Polymarket weather markets.

Uses real LLM API calls when API keys are available (Anthropic or OpenAI),
with a rule-based fallback when no keys are set.

Free-claude-code reference: https://github.com/Alishahryar1/free-claude-code
We call the APIs directly via httpx rather than requiring the free-claude-code
server to be running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_MAX_TRIES = 3
_RETRY_DELAY = 1.0  # seconds
_REQUEST_TIMEOUT = 30.0

_SYSTEM_PROMPT = """\
You are a quantitative prediction-market trading analyst. \
You analyze weather-based trading opportunities on Polymarket and return \
structured JSON assessments.

Respond with ONLY a JSON object, no markdown, no code fences, no explanation \
outside the JSON:
{
  "confidence": <float between 0 and 1>,
  "ev_estimate": <float, expected value from -1.0 to 1.0>,
  "recommendation": <one of "BUY", "SELL", "HOLD">,
  "reasoning": "<one-sentence explanation>"
}"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_user_prompt(
    city: str,
    current_temp: float,
    forecast_temp: float,
    market_price: float,
    direction: str,
) -> str:
    """Build a concise prompt describing the trading opportunity."""
    spread = forecast_temp - current_temp
    return (
        f"City: {city}\n"
        f"Current temperature: {current_temp}°F\n"
        f"Forecast temperature (upcoming): {forecast_temp}°F\n"
        f"Temperature spread (forecast minus current): {spread:+.1f}°F\n"
        f"Market price (YES token): {market_price}\n"
        f"Direction of interest: {direction}\n"
        f"\n"
        f"Assess this as a trading opportunity. Consider:\n"
        f"- Is the forecast significantly different from current temp?\n"
        f"- Is the market price misaligned with the forecast signal?\n"
        f"- How confident should we be given the data?\n"
        f"- What is the expected value of this trade?\n"
        f"Return the JSON assessment only."
    )


async def _call_anthropic(
    prompt: str,
    model: str,
    temperature: float,
) -> dict:
    """Call the Anthropic Messages API and parse the JSON response."""
    api_key = os.environ["ANTHROPIC_API_KEY"]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 512,
        "temperature": temperature,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.post(_ANTHROPIC_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    # Anthropic returns content blocks; concatenate text blocks
    texts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    raw = "\n".join(texts).strip()
    logger.debug("[AI_BRAIN] Anthropic raw response: %s", raw)
    return _parse_json_response(raw)


async def _call_openai(
    prompt: str,
    model: str,
    temperature: float,
) -> dict:
    """Call the OpenAI Chat Completions API and parse the JSON response."""
    api_key = os.environ["OPENAI_API_KEY"]
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 512,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.post(_OPENAI_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    raw = data["choices"][0]["message"]["content"].strip()
    logger.debug("[AI_BRAIN] OpenAI raw response: %s", raw)
    return _parse_json_response(raw)


def _parse_json_response(raw: str) -> dict:
    """Extract and parse a JSON object from an LLM response string.

    Handles common wrappers like markdown code fences.
    """
    text = raw
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove trailing ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("[AI_BRAIN] Failed to parse JSON, attempting extraction")
        # Try to find first { ... } block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(text[start : end + 1])
        else:
            raise ValueError(f"Could not extract JSON from LLM response: {raw!r}")
    # Validate / normalise fields
    result = {
        "confidence": float(parsed.get("confidence", 0.5)),
        "ev_estimate": float(parsed.get("ev_estimate", 0.0)),
        "recommendation": str(parsed.get("recommendation", "HOLD")).upper(),
        "reasoning": str(parsed.get("reasoning", "")),
    }
    # Clamp confidence to [0, 1]
    result["confidence"] = max(0.0, min(1.0, result["confidence"]))
    return result


def _rule_based_analysis(
    current_temp: float,
    forecast_temp: float,
    market_price: float,
    direction: str,
) -> dict:
    """Simple rule-based fallback when no LLM API key is available."""
    temp_diff = forecast_temp - current_temp
    if direction.upper() == "YES":
        if temp_diff > 5 and market_price < 0.6:
            return {
                "confidence": 0.7,
                "ev_estimate": round(
                    (1.0 - market_price) * 0.7 - market_price * 0.3, 4
                ),
                "recommendation": "BUY",
                "reasoning": (
                    f"Forecasting {temp_diff:+.1f}F above current "
                    f"({current_temp}F -> {forecast_temp}F) for {direction}; "
                    f"market price {market_price} appears underpriced."
                ),
            }
    elif direction.upper() == "NO":
        if temp_diff < -5 and market_price > 0.4:
            return {
                "confidence": 0.6,
                "ev_estimate": round(
                    market_price * 0.6 - (1.0 - market_price) * 0.4, 4
                ),
                "recommendation": "BUY",
                "reasoning": (
                    f"Forecasting {temp_diff:+.1f}F below current "
                    f"({current_temp}F -> {forecast_temp}F) for {direction}; "
                    f"market price {market_price} suggests room to sell."
                ),
            }
    # Default: no clear edge
    return {
        "confidence": 0.3,
        "ev_estimate": 0.0,
        "recommendation": "HOLD",
        "reasoning": (
            f"No strong signal: temp spread {temp_diff:+.1f}F, "
            f"market={market_price}, direction={direction}."
        ),
    }


# ---------------------------------------------------------------------------
# AIBrain – main class
# ---------------------------------------------------------------------------


class AIBrain:
    """AI decision engine for Polymarket weather trade analysis.

    Prefers Anthropic > OpenAI > rule-based fallback, depending on which
    API keys are set in the environment.

    Parameters
    ----------
    model : str
        LLM model identifier.  For Anthropic use the full model name
        (e.g. ``"claude-sonnet-4-20250514"``); for OpenAI use e.g.
        ``"gpt-4o"``.
    temperature : float
        Sampling temperature (lower = more deterministic).
    """

    def __init__(
        self,
        model: str = "anthropic/claude-sonnet-4-20250514",
        temperature: float = 0.1,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.conversation_history: list[dict[str, str]] = []
        self._backend: str = self._detect_backend()
        logger.info(
            "[AI_BRAIN] initialised model=%s temperature=%.2f backend=%s",
            self.model,
            self.temperature,
            self._backend,
        )

    # -- public API ----------------------------------------------------------

    async def analyze_opportunity(
        self,
        city: str,
        current_temp: float,
        forecast_temp: float,
        market_price: float,
        direction: str,
    ) -> dict:
        """Analyze a single trading opportunity.

        Returns a dict with keys: ``confidence``, ``ev_estimate``,
        ``recommendation``, ``reasoning``.
        """
        prompt = _build_user_prompt(
            city, current_temp, forecast_temp, market_price, direction
        )
        # Record in conversation history
        self.conversation_history.append(
            {"role": "user", "content": prompt}
        )

        result: dict
        if self._backend == "anthropic":
            result = await self._with_retry(
                _call_anthropic, prompt, self.model, self.temperature
            )
        elif self._backend == "openai":
            result = await self._with_retry(
                _call_openai, prompt, self.model, self.temperature
            )
        else:
            logger.info("[AI_BRAIN] using rule-based fallback for %s", city)
            result = _rule_based_analysis(
                current_temp, forecast_temp, market_price, direction
            )

        # Store assistant response
        self.conversation_history.append(
            {"role": "assistant", "content": json.dumps(result)}
        )

        logger.info(
            "[AI_BRAIN] %s => %s (conf=%.2f ev=%.4f)",
            city,
            result["recommendation"],
            result["confidence"],
            result["ev_estimate"],
        )
        return result

    async def batch_analyze(
        self, opportunities: list[dict[str, Any]]
    ) -> list[dict]:
        """Analyze multiple opportunities concurrently.

        Each item in *opportunities* must contain keys:
        ``city``, ``current_temp``, ``forecast_temp``, ``market_price``,
        ``direction``.

        Returns a list of result dicts in the same order as the input.
        """
        semaphore = asyncio.Semaphore(5)  # cap concurrency

        async def _limited(op: dict) -> dict:
            async with semaphore:
                return await self.analyze_opportunity(
                    city=op["city"],
                    current_temp=op["current_temp"],
                    forecast_temp=op["forecast_temp"],
                    market_price=op["market_price"],
                    direction=op["direction"],
                )

        tasks = [_limited(op) for op in opportunities]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(results)

    # -- internals -----------------------------------------------------------

    def _detect_backend(self) -> str:
        """Return the best available backend: ``'anthropic'``, ``'openai'``, or ``'rule_based'``."""
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        return "rule_based"

    @staticmethod
    async def _with_retry(fn, *args, **kwargs) -> dict:
        """Call *fn* with retries on transient errors."""
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_TRIES + 1):
            try:
                return await fn(*args, **kwargs)
            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "[AI_BRAIN] attempt %d/%d failed: %s", attempt, _MAX_TRIES, exc
                )
                if attempt < _MAX_TRIES:
                    await asyncio.sleep(_RETRY_DELAY * attempt)
        raise RuntimeError(
            f"All {_MAX_TRIES} LLM calls failed; last error: {last_exc}"
        ) from last_exc

    def __repr__(self) -> str:
        return (
            f"<AIBrain model={self.model!r} temp={self.temperature} "
            f"backend={self._backend}>"
        )
