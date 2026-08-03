"""
Community sentiment scanner for Polymarket weather markets.

Monitors public data sources for city-level sentiment:
  1. Polymarket market probability shifts (on-chain, public)
  2. Twitter/X mentions of city + weather keywords
  3. Simple keyword-based scoring

Usage:
    from polybot.sentiment import get_sentiment_score
    score = get_sentiment_score("bangkok")  # Returns -1.0 to +1.0
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SENTIMENT_CACHE_PATH = "/polybot-data/sentiment_cache.json"
SENTIMENT_CACHE_TTL = 300  # 5 minutes

# Positive keywords (bullish on higher temps)
POSITIVE_KEYWORDS = [
    "heat", "hot", "warm", "burning", "scorcher", "boiling",
    "bullish", "over", "above", "exceed", "record", "heatwave",
    "stretch", "sizzling", "sweltering", "scorching",
]

# Negative keywords (bearish on higher temps / expecting cooler)
NEGATIVE_KEYWORDS = [
    "cold", "cool", "freezing", "chilly", "below", "under",
    "miss", "under", "rain", "cloudy", "mild", "moderate",
    "expecting lower", "bearish", "won't reach", "won't hit",
]

# City name variants for matching
CITY_ALIASES: dict[str, list[str]] = {
    "london": ["london", "ldn"],
    "nyc": ["new york", "nyc", "new york city"],
    "seoul": ["seoul"],
    "hong_kong": ["hong kong", "hk", "hongkong"],
    "shanghai": ["shanghai"],
    "beijing": ["beijing", "bj"],
    "mumbai": ["mumbai", "bombay"],
    "istanbul": ["istanbul"],
    "mexico_city": ["mexico city", "cdmx", "mexico"],
    "jakarta": ["jakarta"],
    "chongqing": ["chongqing", "cq"],
    "bangkok": ["bangkok", "bkk"],
    "manila": ["manila"],
    "kuala_lumpur": ["kuala lumpur", "kl", "kualalumpur"],
    "ho_chi_minh_city": ["ho chi minh", "saigon", "hcmc", "ho chi minh city"],
    "taipei": ["taipei"],
    "shenzhen": ["shenzhen"],
    "guangzhou": ["guangzhou", "canton"],
    "chicago": ["chicago", "chi"],
    "dallas": ["dallas"],
    "atlanta": ["atlanta"],
    "miami": ["miami"],
    "cape_town": ["cape town", "capetown"],
    "buenos_aires": ["buenos aires", "ba", "buenosaires"],
}


def _load_cache() -> dict:
    """Load sentiment cache from disk."""
    try:
        if os.path.exists(SENTIMENT_CACHE_PATH):
            with open(SENTIMENT_CACHE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(cache: dict):
    """Save sentiment cache to disk."""
    try:
        os.makedirs(os.path.dirname(SENTIMENT_CACHE_PATH), exist_ok=True)
        with open(SENTIMENT_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _score_text(text: str) -> float:
    """
    Score a piece of text for weather sentiment.

    Returns:
        Float from -1.0 (very bearish) to +1.0 (very bullish).
    """
    text_lower = text.lower()
    pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
    neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_lower)

    total = pos_count + neg_count
    if total == 0:
        return 0.0

    # Normalize to [-1, 1]
    raw = (pos_count - neg_count) / total
    # Scale by confidence (more mentions = more confident)
    confidence = min(total / 5.0, 1.0)  # Max confidence at 5+ keyword hits
    return round(raw * confidence, 3)


def get_on_chain_sentiment(city: str, market_data: Optional[dict] = None) -> float:
    """
    Derive sentiment from on-chain Polymarket data.

    If a market's YES price has moved significantly in the last N blocks,
    that indicates crowd sentiment shift.

    Args:
        city: City slug.
        market_data: Optional dict with "price_change_24h" (float, 0-1 range).

    Returns:
        Sentiment score [-1, 1].
    """
    if market_data is None:
        return 0.0

    price_change = market_data.get("price_change_24h", 0.0)
    if abs(price_change) < 0.02:
        return 0.0

    # Price went up -> bullish on YES (higher temps)
    sentiment = max(-1.0, min(1.0, price_change * 5))  # 20% move = max sentiment
    logger.debug(f"[SENTIMENT] {city} on-chain: price_change={price_change:+.3f} -> sentiment={sentiment:+.3f}")
    return round(sentiment, 3)


def get_sentiment_score(
    city: str,
    market_data: Optional[dict] = None,
    use_cache: bool = True,
) -> float:
    """
    Get overall sentiment score for a city.
    Combines on-chain data and keyword analysis.

    Returns:
        Float from -1.0 (bearish/expecting cooler) to +1.0 (bullish/expecting hotter).
    """
    # Check cache
    cache = _load_cache()
    cache_key = city.lower()
    if use_cache and cache_key in cache:
        cached = cache[cache_key]
        if time.time() - cached.get("ts", 0) < SENTIMENT_CACHE_TTL:
            return cached.get("score", 0.0)

    # On-chain sentiment
    on_chain = get_on_chain_sentiment(city, market_data)

    # In production, we'd also scan Twitter/Telegram here
    # For now, on-chain is the most reliable signal
    score = on_chain

    # Cache result
    cache[cache_key] = {"score": score, "ts": time.time()}
    _save_cache(cache)

    logger.info(f"[SENTIMENT] {city}: score={score:+.3f}")
    return score


def get_sentiment_kelly_multiplier(city: str, model_edge_positive: bool, sentiment_score: Optional[float] = None) -> float:
    """
    Compute Kelly multiplier based on sentiment alignment with model.

    If sentiment > 0.5 and model edge is positive -> increase bet by 20%
    If sentiment < -0.5 and model edge is positive -> reduce bet by 20%

    Returns:
        Kelly multiplier (1.0 = no change, 1.2 = +20%, 0.8 = -20%).
    """
    if sentiment_score is None:
        sentiment_score = get_sentiment_score(city)

    if not model_edge_positive:
        # Model says sell/short, sentiment says buy -> reduce size
        if sentiment_score > 0.5:
            logger.info(f"[SENTIMENT] {city}: model short + bullish sentiment -> Kelly * 0.8")
            return 0.8
        return 1.0

    # Model says buy
    if sentiment_score > 0.5:
        logger.info(f"[SENTIMENT] {city}: model + sentiment aligned (bullish) -> Kelly * 1.2")
        return 1.2
    elif sentiment_score < -0.5:
        logger.info(f"[SENTIMENT] {city}: model bullish + bearish sentiment -> Kelly * 0.8")
        return 0.8

    return 1.0
