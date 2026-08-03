"""
LightGBM training framework for weather prediction.

Trains a gradient boosting model on resolved trades + forecast features
to predict settlement probability. Activated after 100 resolved trades.

Usage:
    result = run_training_if_ready(min_trades=100)
    # Trains if enough data, saves model to /polybot-data/lgbm_model.pkl
"""

from __future__ import annotations

import json
import logging
import os
import pickle
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

MODEL_PATH = "/polybot-data/lgbm_model.pkl"
MIN_TRAINING_TRADES = 100


def load_resolved_trades(min_trades: int = MIN_TRAINING_TRADES) -> list[dict]:
    """
    Load resolved paper trades from Redis.
    Only returns wins/losses with valid profit data.
    """
    import redis as _redis_mod

    try:
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
    except Exception:
        return []

    raw = r.lrange("paper_trades", 0, 999)
    resolved = []
    for item in raw:
        try:
            t = json.loads(item)
            if t.get("status") in ("resolved", "closed") and t.get("profit_pct"):
                resolved.append(t)
        except Exception:
            pass

    logger.info("[LGBM] Loaded %d resolved trades", len(resolved))
    return resolved


def build_features(trades: list[dict]) -> tuple[list[list[float]], list[int]]:
    """
    Build feature matrix and labels from resolved trades.

    Features:
        - hour_of_day (0-23)
        - month (1-12)
        - city_encoded (hash)
        - model_spread_f
        - model_confidence
        - entry_price
        - bucket_threshold_f
        - ensemble_std
        - settlement_correction
        - day_of_week (0-6)
        - is_peak_hour (0/1)

    Labels:
        - 1 if trade won, 0 if lost
    """
    X, y = [], []

    for t in trades:
        try:
            # Parse timestamp
            ts_str = t.get("timestamp", "")
            if ts_str:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                hour = ts.hour
                month = ts.month
                dow = ts.weekday()
            else:
                hour, month, dow = 12, 6, 0

            city = t.get("city", "")
            city_hash = hash(city) % 100 / 100.0  # Simple encoding

            # Get metrics from Redis
            import redis as _redis_mod
            r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
            metrics_raw = r.hgetall(f"city_metrics:{city}") if r else {}
            metrics = {}
            if metrics_raw:
                for k, v in metrics_raw.items():
                    kk = k.decode() if isinstance(k, bytes) else str(k)
                    vv = v.decode() if isinstance(v, bytes) else str(v)
                    try:
                        metrics[kk] = float(vv)
                    except ValueError:
                        metrics[kk] = vv

            model_spread = metrics.get("model_spread_f", 2.0)
            model_conf = metrics.get("agreement_confidence", 0.5)
            ensemble_std = metrics.get("ensemble_std_f", 2.5)

            try:
                entry_price = float(t.get("entry_price", 0.5))
            except (ValueError, TypeError):
                entry_price = 0.5

            bucket = t.get("bucket", "")
            # Extract threshold from bucket name
            threshold_f = 0.0
            import re
            m = re.search(r"(\d+)", bucket)
            if m:
                threshold_f = float(m.group(1))

            # Settlement correction
            try:
                from polybot.settlement_corrections import get_settlement_correction
                correction = get_settlement_correction(city)
            except Exception:
                correction = 0.0

            is_peak = 1 if 13 <= hour <= 17 else 0

            features = [
                hour / 23.0,
                month / 12.0,
                city_hash,
                model_spread,
                model_conf,
                entry_price,
                threshold_f / 100.0,
                ensemble_std,
                correction,
                dow / 6.0,
                is_peak,
            ]

            # Label: 1 = win, 0 = loss
            profit = float(t.get("profit_pct", 0))
            won = 1 if profit > 0 else 0

            X.append(features)
            y.append(won)

        except Exception as e:
            logger.debug("[LGBM] Skipping trade: %s", e)
            continue

    return X, y


def train_model(X: list[list[float]], y: list[int]) -> Optional[object]:
    """
    Train a LightGBM classifier on the feature matrix.
    Returns the trained model or None on failure.
    """
    try:
        import lightgbm as lgb
        import numpy as np
    except ImportError:
        logger.warning("[LGBM] lightgbm not installed — skipping training")
        return None

    X_arr = np.array(X)
    y_arr = np.array(y)

    if len(X_arr) < MIN_TRAINING_TRADES:
        logger.info("[LGBM] Not enough data: %d < %d", len(X_arr), MIN_TRAINING_TRADES)
        return None

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "n_estimators": 200,
        "min_child_samples": 10,
    }

    # Time-based split: last 20% for validation
    split_idx = int(len(X_arr) * 0.8)
    X_train, X_val = X_arr[:split_idx], X_arr[split_idx:]
    y_train, y_val = y_arr[:split_idx], y_arr[split_idx:]

    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(20, verbose=False)],
    )

    # Compute validation accuracy
    val_pred = model.predict(X_val)
    accuracy = (val_pred == y_val).mean()
    logger.info("[LGBM] Trained on %d samples, val accuracy: %.1f%%", len(X_arr), accuracy * 100)

    return model


def save_model(model, path: str = MODEL_PATH):
    """Save trained model to disk."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(model, f)
        logger.info("[LGBM] Model saved to %s", path)
    except Exception as e:
        logger.error("[LGBM] Failed to save model: %s", e)


def load_model(path: str = MODEL_PATH) -> Optional[object]:
    """Load a trained model from disk."""
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                model = pickle.load(f)
            logger.info("[LGBM] Model loaded from %s", path)
            return model
    except Exception as e:
        logger.error("[LGBM] Failed to load model: %s", e)
    return None


def predict_probability(model, features: list[float]) -> float:
    """
    Predict win probability for a trade using the trained model.
    Returns probability (0-1) of the trade being a winner.
    """
    try:
        import numpy as np
        X = np.array([features])
        proba = model.predict_proba(X)[0]
        # Return probability of class 1 (win)
        return float(proba[1]) if len(proba) > 1 else float(proba[0])
    except Exception as e:
        logger.debug("[LGBM] Prediction error: %s", e)
        return 0.5  # uninformative prior


def run_training_if_ready(min_trades: int = MIN_TRAINING_TRADES) -> dict:
    """
    Main entry point: check if enough data, train if ready.
    Called by the weekly audit cron.
    """
    trades = load_resolved_trades(min_trades)

    if len(trades) < min_trades:
        msg = f"Not enough resolved trades: {len(trades)}/{min_trades}"
        logger.info("[LGBM] %s", msg)
        return {"status": "skipped", "reason": msg, "trades": len(trades)}

    X, y = build_features(trades)

    if len(X) < min_trades:
        msg = f"Not enough valid feature rows: {len(X)}/{min_trades}"
        logger.info("[LGBM] %s", msg)
        return {"status": "skipped", "reason": msg, "valid_features": len(X)}

    model = train_model(X, y)

    if model:
        save_model(model)
        return {"status": "trained", "samples": len(X), "model_path": MODEL_PATH}
    else:
        return {"status": "failed", "reason": "training error"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_training_if_ready()
    print(result)
