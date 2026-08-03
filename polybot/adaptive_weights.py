"""
Adaptive Weighting Engine for the multi-model ensemble.

Periodically recomputes model weights using inverse-RMSE weighting
over the last N days of forecast history, then stores the result
in Redis so the ensemble and prediction engine can use it.

Usage:
    from polybot.adaptive_weights import weekly_weight_update, compute_rmse_per_model
    weights = weekly_weight_update()  # Called from Modal weekly cron
"""

import json
import os
import sqlite3
from pathlib import Path

import numpy as np

DB_PATH = "/polybot-data/history.db"

# Default fallback weights if no history exists
DEFAULT_WEIGHTS = {
    "ecmwf": 0.40,
    "weatherstack": 0.35,
    "openmeteo": 0.25,
}


def compute_rmse_per_model(days: int = 30) -> dict[str, float]:
    """
    Compute RMSE for each model over the last N days from the forecast history DB.

    Args:
        days: Number of days of history to consider (default 30)

    Returns:
        Dict of {model_name: rmse_value}. Empty dict if no history.
    """
    db = Path(DB_PATH)
    if not db.exists():
        print(f"[ADAPTIVE] History DB not found at {DB_PATH}")
        return {}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            SELECT model_name, AVG((forecast_f - actual_f) * (forecast_f - actual_f)) as mse
            FROM forecasts
            WHERE date >= date('now', ?)
            GROUP BY model_name
        """, (f'-{days} days',))
        rows = c.fetchall()
    except sqlite3.OperationalError as e:
        print(f"[ADAPTIVE] DB query error (table missing?): {e}")
        rows = []
    finally:
        conn.close()

    if not rows:
        return {}

    rmse = {model: np.sqrt(mse) for model, mse in rows}
    print(f"[ADAPTIVE] RMSE per model (last {days}d): {rmse}")
    return rmse


def update_ensemble_weights(rmse_dict: dict[str, float]) -> dict[str, float]:
    """
    Compute new ensemble weights using inverse-RMSE weighting.

    Lower RMSE -> higher weight. Weights are normalized to sum to 1.0.

    Args:
        rmse_dict: {model_name: rmse_value}

    Returns:
        Dict of {model_name: normalized_weight}
    """
    if not rmse_dict:
        print("[ADAPTIVE] No RMSE data, using default weights")
        return dict(DEFAULT_WEIGHTS)

    inv = {m: 1.0 / max(float(rmse), 0.1) for m, rmse in rmse_dict.items()}
    total = sum(inv.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)

    weights = {m: round(v / total, 4) for m, v in inv.items()}

    # Store in Redis
    try:
        import redis as _redis
        r = _redis.from_url(os.environ["REDIS_URL"])
        r.set("ensemble_weights", json.dumps(weights))
        print(f"[ADAPTIVE] Weights stored in Redis: {weights}")
    except Exception as e:
        print(f"[ADAPTIVE] Redis write error: {e}")

    return weights


def get_current_weights() -> dict[str, float]:
    """
    Get current ensemble weights from Redis, or defaults if not set.

    Returns:
        Dict of {model_name: weight}
    """
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", ""))
        raw = r.get("ensemble_weights")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return dict(DEFAULT_WEIGHTS)


def weekly_weight_update() -> dict[str, float]:
    """
    Full weekly weight update pipeline:
    1. Compute RMSE per model over last 30 days
    2. Update ensemble weights via inverse-RMSE
    3. Store in Redis

    Returns:
        New weight dict {model_name: weight}
    """
    print("[ADAPTIVE] Starting weekly weight update...")
    rmse = compute_rmse_per_model(30)
    new_weights = update_ensemble_weights(rmse)
    print(f"[ADAPTIVE] New weights: {new_weights}")
    return new_weights
