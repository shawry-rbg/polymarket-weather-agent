"""
Dynamic Error Balancing (DEB) weighted model blending.

Replaces the fixed-weight ensemble average with per-city, per-hour,
per-model inverse-MAE weights. Models with lower recent error get
higher weight.

Weights are computed from a rolling 30-day MAE stored in SQLite.
If insufficient history, falls back to default weights.

Usage:
    weights = get_deb_weights("london", hour=14)
    # {"ecmwf": 0.52, "openmeteo": 0.33, "weatherstack": 0.15}
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "/polybot-data/deb_weights.db"

# Default weights when no history available
DEFAULT_WEIGHTS: dict[str, float] = {
    "ecmwf": 0.40,
    "openmeteo": 0.25,
    "weatherstack": 0.35,
    "gfs": 0.30,
    "hrrr": 0.35,
}

# Minimum number of samples before DEB kicks in
MIN_SAMPLES = 10


def _get_db() -> sqlite3.Connection:
    """Get or create the DEB weights database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_slug TEXT NOT NULL,
            model TEXT NOT NULL,
            hour INTEGER NOT NULL,
            mae REAL NOT NULL,
            samples INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_model_errors_city_hour
        ON model_errors(city_slug, hour, model)
    """)
    conn.commit()
    return conn


def record_error(city_slug: str, model: str, hour: int, error_f: float):
    """
    Record a forecast error for a model/city/hour combination.

    This is called after market resolution to update the rolling error stats.
    """
    try:
        conn = _get_db()
        now = datetime.now(timezone.utc).isoformat()

        # Check if record exists
        row = conn.execute(
            "SELECT mae, samples FROM model_errors WHERE city_slug=? AND model=? AND hour=?",
            (city_slug, model, hour),
        ).fetchone()

        if row:
            old_mae, old_samples = row
            # Exponential moving average with alpha=0.3
            alpha = 0.3
            new_mae = alpha * abs(error_f) + (1 - alpha) * old_mae
            new_samples = old_samples + 1
            conn.execute(
                "UPDATE model_errors SET mae=?, samples=?, updated_at=? WHERE city_slug=? AND model=? AND hour=?",
                (new_mae, new_samples, now, city_slug, model, hour),
            )
        else:
            conn.execute(
                "INSERT INTO model_errors (city_slug, model, hour, mae, samples, updated_at) VALUES (?, ?, ?, ?, 1, ?)",
                (city_slug, model, hour, abs(error_f), now),
            )

        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("[DEB] Error recording: %s", e)


def get_deb_weights(city_slug: str, hour: int, available_models: list[str] | None = None) -> dict[str, float]:
    """
    Compute DEB weights for a city/hour combination.

    Weight = 1 / MAE for each model, then normalize to sum to 1.0.

    If insufficient history, falls back to DEFAULT_WEIGHTS.
    """
    if available_models is None:
        available_models = list(DEFAULT_WEIGHTS.keys())

    try:
        conn = _get_db()
        weights = {}
        total_inv_mae = 0.0

        for model in available_models:
            row = conn.execute(
                "SELECT mae, samples FROM model_errors WHERE city_slug=? AND model=? AND hour=?",
                (city_slug, model, hour),
            ).fetchone()

            if row and row[1] >= MIN_SAMPLES:
                mae = max(row[0], 0.1)  # floor to avoid division by zero
                inv_mae = 1.0 / mae
                weights[model] = inv_mae
                total_inv_mae += inv_mae
            else:
                # Use default weight scaled by 1/MAE estimate
                default = DEFAULT_WEIGHTS.get(model, 0.2)
                inv_mae = default  # Use default as pseudo-inv-mae
                weights[model] = inv_mae
                total_inv_mae += inv_mae

        conn.close()

        # Normalize
        if total_inv_mae > 0:
            weights = {m: w / total_inv_mae for m, w in weights.items()}
        else:
            # Equal weights fallback
            n = len(available_models)
            weights = {m: 1.0 / n for m in available_models} if n > 0 else {}

        return weights

    except Exception as e:
        logger.debug("[DEB] Error computing weights: %s", e)
        # Fallback to equal weights
        n = len(available_models)
        return {m: 1.0 / n for m in available_models} if n > 0 else {}


def get_weighted_ensemble_temp(model_temps: dict[str, float], city_slug: str, hour: int) -> tuple[float, dict[str, float]]:
    """
    Compute weighted ensemble temperature using DEB weights.

    Args:
        model_temps: {model_name: temp_f}
        city_slug: City for weight lookup
        hour: Local hour for weight lookup

    Returns:
        (weighted_temp_f, weights_used)
    """
    if not model_temps:
        return 70.0, {}

    available = list(model_temps.keys())
    weights = get_deb_weights(city_slug, hour, available)

    weighted_sum = sum(model_temps[m] * weights.get(m, 0) for m in available)
    weight_total = sum(weights.get(m, 0) for m in available)

    if weight_total > 0:
        return round(weighted_sum / weight_total, 1), weights
    else:
        # Simple average fallback
        return round(sum(model_temps.values()) / len(model_temps), 1), {m: 1.0 / len(model_temps) for m in available}
