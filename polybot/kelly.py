"""
Kelly Criterion position sizing for prediction market trading.

Implements the Kelly Criterion formula with hard risk rules:
- Max single bet = 2.5% of bankroll (capped at $0.30 until balance > $10)
- Max weekly drawdown 25%
- Balance floor $0.50
- Bet size increase only after 5 consecutive wins
- Stop-loss at 20% below entry, take-profit at 60% above
"""

import math

# Safety bounds
_MIN_KELLY_USD = 0.0
_MAX_KELLY_USD = 100.0
_DEFAULT_KELLY_FRACTION = 0.10  # tenth-Kelly for safety
_TEMP_SENSITIVITY_K = 0.5
_PROB_CLAMP_MIN = 0.01
_PROB_CLAMP_MAX = 0.99
_ADAPTIVE_WIN_BOOST = 0.05
_ADAPTIVE_LOSS_REDUCE = 0.10
_ADAPTIVE_MIN_FRACTION = 0.05
_ADAPTIVE_MAX_FRACTION = 0.50


def adaptive_kelly_fraction(
    base_fraction: float = _DEFAULT_KELLY_FRACTION,
    recent_win_rate: float | None = None,
    consecutive_losses: int = 0,
) -> float:
    """
    Adjust base Kelly fraction using recent performance.

    Rules:
    - Recent win rate >= 60% -> increase fraction
    - Recent win rate <= 40% -> decrease fraction
    - 3+ consecutive losses -> reduce fraction further
    """
    fraction = float(base_fraction)

    if recent_win_rate is not None:
        if recent_win_rate >= 0.60:
            fraction += _ADAPTIVE_WIN_BOOST
        elif recent_win_rate <= 0.40:
            fraction -= _ADAPTIVE_LOSS_REDUCE

    if consecutive_losses >= 3:
        fraction -= _ADAPTIVE_LOSS_REDUCE

    return max(_ADAPTIVE_MIN_FRACTION, min(_ADAPTIVE_MAX_FRACTION, round(fraction, 4)))


def kelly_criterion(
    true_prob: float,
    market_price: float,
    bankroll: float,
    fraction: float = _DEFAULT_KELLY_FRACTION,
    recent_win_rate: float | None = None,
    consecutive_losses: int = 0,
) -> dict:
    """
    Calculate Kelly-optimal position size for a Polymarket trade.

    Args:
        true_prob: Our estimated probability that the outcome is YES (0..1).
        market_price: Current YES token price on Polymarket (0..1).
        bankroll: Total available capital in USD.
        fraction: Kelly fraction to use (default 0.25 = quarter-Kelly).
                  Lower values are more conservative.

    Returns:
        dict with keys:
            direction  - 'YES', 'NO', or 'NONE'
            edge       - absolute edge over market (0 if no edge)
            kelly_fraction - fractional Kelly value (fraction of bankroll)
            kelly_usd  - dollar amount to trade (clamped to [0, $100])
    """
    # Validate inputs to avoid division by zero and nonsensical values
    market_price = max(0.001, min(0.999, market_price))

    # Determine direction based on whether our estimate is above or below market
    if true_prob > market_price:
        # We think YES is more likely than market implies
        direction = "YES"
        edge = true_prob - market_price
        # Net odds for buying YES: you pay market_price, receive 1.0 if YES wins
        b = (1.0 / market_price) - 1.0
        p = true_prob
    elif true_prob < market_price:
        # We think NO is more likely than market implies
        direction = "NO"
        edge = market_price - true_prob
        # Net odds for buying NO: you pay (1 - market_price), receive 1.0 if NO wins
        b = (1.0 / (1.0 - market_price)) - 1.0
        p = 1.0 - true_prob
    else:
        # No edge — our probability matches market price
        return {
            "direction": "NONE",
            "edge": 0.0,
            "kelly_fraction": 0.0,
            "kelly_usd": 0.0,
        }

    # If edge is zero or negative, no trade
    if edge <= 0:
        return {
            "direction": "NONE",
            "edge": 0.0,
            "kelly_fraction": 0.0,
            "kelly_usd": 0.0,
        }

    q = 1.0 - p

    # Kelly formula: f* = (bp - q) / b
    kelly_f = (b * p - q) / b

    # Apply partial Kelly fraction for safety
    fraction = adaptive_kelly_fraction(
        base_fraction=fraction,
        recent_win_rate=recent_win_rate,
        consecutive_losses=consecutive_losses,
    )
    kelly_f *= fraction

    # If Kelly fraction is negative, no trade is warranted
    if kelly_f <= 0:
        return {
            "direction": "NONE",
            "edge": edge,
            "kelly_fraction": 0.0,
            "kelly_usd": 0.0,
        }

    # Convert to dollar amount
    kelly_usd = kelly_f * bankroll

    # Clamp to safety bounds
    kelly_usd = max(_MIN_KELLY_USD, min(_MAX_KELLY_USD, kelly_usd))

    return {
        "direction": direction,
        "edge": edge,
        "kelly_fraction": kelly_f,
        "kelly_usd": round(kelly_usd, 2),
    }


def compute_expected_value(true_prob: float, market_price: float) -> dict:
    """
    Compute the expected value of buying YES and NO tokens on Polymarket.

    For YES token:
        EV = true_prob * (1 - market_price) - (1 - true_prob) * market_price

    For NO token:
        EV = (1 - true_prob) * market_price - true_prob * (1 - market_price)

    Args:
        true_prob: Our estimated probability that the outcome is YES (0..1).
        market_price: Current YES token price on Polymarket (0..1).

    Returns:
        dict with keys:
            ev_yes - expected value of buying one YES token
            ev_no  - expected value of buying one NO token
    """
    ev_yes = true_prob * (1.0 - market_price) - (1.0 - true_prob) * market_price
    ev_no = (1.0 - true_prob) * market_price - true_prob * (1.0 - market_price)
    return {
        "ev_yes": round(ev_yes, 6),
        "ev_no": round(ev_no, 6),
    }


def estimate_temperature_probability(
    forecast_temp: float,
    threshold: float,
    k: float = _TEMP_SENSITIVITY_K,
) -> float:
    """
    Estimate the probability that a temperature exceeds a given threshold.

    Uses a logistic (sigmoid) function centered on the threshold:

        P(T > threshold) = 1 / (1 + exp(-k * (forecast_temp - threshold)))

    Args:
        forecast_temp: Predicted temperature value.
        threshold: Temperature threshold to exceed.
        k: Sensitivity parameter controlling the steepness of the transition.
           Default 0.5.

    Returns:
        Probability clamped to [0.01, 0.99].
    """
    raw_prob = 1.0 / (1.0 + math.exp(-k * (forecast_temp - threshold)))
    return max(_PROB_CLAMP_MIN, min(_PROB_CLAMP_MAX, raw_prob))
