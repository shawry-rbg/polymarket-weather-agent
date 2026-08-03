"""
Probability calibration module for Polybot weather predictions.

Provides Platt scaling, isotonic regression, Brier score computation,
calibration error analysis, and historical base rate checking.
"""

import json
import logging
import math
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standalone calibrators
# ---------------------------------------------------------------------------

def platt_scale(raw_prob: float, a: float = 1.0, b: float = 0.0) -> float:
    """Apply Platt scaling to a raw probability.

    P_calibrated = 1 / (1 + exp(-a * raw_prob - b))

    Args:
        raw_prob: Raw probability in [0, 1].
        a: Scale parameter (slope).
        b: Shift parameter (intercept).

    Returns:
        Calibrated probability.
    """
    z = -a * raw_prob - b
    # Guard against overflow
    if z > 500:
        return 0.0
    if z < -500:
        return 1.0
    return 1.0 / (1.0 + math.exp(z))


def isotonic_calibrate(raw_probs: list[float], outcomes: list[int],
                       n_bins: int = 10) -> callable:
    """Build a piecewise-constant calibration map via isotonic-style binning.

    Raw predictions are divided into *n_bins* equal-width bins.  Within each
    bin the calibrated probability is set to the observed fraction of positive
    outcomes.  The returned callable maps any raw probability to its bin's
    empirical rate, clamping to the global mean for empty bins.

    Args:
        raw_probs: List of predicted probabilities.
        outcomes:  List of binary outcomes (0 or 1).
        n_bins:    Number of calibration bins (default 10).

    Returns:
        A callable(raw_prob) -> calibrated_prob.
    """
    if not raw_probs or len(raw_probs) != len(outcomes):
        raise ValueError("raw_probs and outcomes must be non-empty and equal length")

    pairs = list(zip(raw_probs, outcomes))
    # Sort by prediction value for monotonic bin edges
    pairs.sort(key=lambda x: x[0])

    global_rate = sum(outcomes) / len(outcomes)
    edges = [i / n_bins for i in range(n_bins + 1)]  # [0, 0.1, ..., 1.0]
    bin_rates: list[Optional[float]] = [None] * n_bins
    bin_counts: list[int] = [0] * n_bins

    for prob, outcome in pairs:
        idx = min(int(prob * n_bins), n_bins - 1)
        if bin_rates[idx] is None:
            bin_rates[idx] = 0.0
        bin_rates[idx] += outcome  # type: ignore[operator]
        bin_counts[idx] += 1

    # Convert sums to means
    for i in range(n_bins):
        if bin_counts[i] > 0 and bin_rates[i] is not None:
            bin_rates[i] = bin_rates[i] / bin_counts[i]  # type: ignore[assignment]
        else:
            bin_rates[i] = global_rate

    def mapper(raw_prob: float) -> float:
        idx = min(int(raw_prob * n_bins), n_bins - 1)
        idx = max(idx, 0)
        return bin_rates[idx]  # type: ignore[return-value]

    return mapper


# ---------------------------------------------------------------------------
# Brier score & calibration error
# ---------------------------------------------------------------------------

def compute_brier_score(predictions: list[float],
                        outcomes: list[int]) -> float:
    """Compute the mean Brier score.

    Brier = (1/N) * sum((prediction - outcome)^2)

    Args:
        predictions: Probabilistic predictions in [0, 1].
        outcomes:    Observed binary outcomes (0 or 1).

    Returns:
        Brier score (lower is better).
    """
    if len(predictions) != len(outcomes) or not predictions:
        raise ValueError("predictions and outcomes must be non-empty and equal length")

    total = sum((p - o) ** 2 for p, o in zip(predictions, outcomes))
    return total / len(predictions)


def compute_calibration_error(predictions: list[float],
                              outcomes: list[int],
                              n_bins: int = 10) -> dict:
    """Compute calibration curve statistics.

    Bins equal-width bins over [0, 1] and computes for each:
      - mean_predicted: average prediction in the bin
      - fraction_positive:  observed positive rate
      - count: number of samples

    Also returns the Expected Calibration Error (ECE).

    Args:
        predictions: Probabilistic predictions.
        outcomes:    Binary outcomes.
        n_bins:      Number of bins.

    Returns:
        Dict with keys:
            bins: list of {bin_index, mean_predicted, fraction_positive, count}
            ece:  Expected Calibration Error (weighted mean |pred - actual|)
            brier: Brier score
    """
    if len(predictions) != len(outcomes) or not predictions:
        raise ValueError("predictions and outcomes must be non-empty and equal length")

    bucket_pred: list[list[float]] = [[] for _ in range(n_bins)]
    bucket_out: list[list[int]] = [[] for _ in range(n_bins)]

    for p, o in zip(predictions, outcomes):
        idx = min(int(p * n_bins), n_bins - 1)
        bucket_pred[idx].append(p)
        bucket_out[idx].append(o)

    bins = []
    ece = 0.0
    total = len(predictions)

    for i in range(n_bins):
        count = len(bucket_pred[i])
        if count == 0:
            bins.append({
                "bin_index": i,
                "mean_predicted": None,
                "fraction_positive": None,
                "count": 0,
            })
            continue
        mean_pred = sum(bucket_pred[i]) / count
        frac_pos = sum(bucket_out[i]) / count
        ece += (count / total) * abs(mean_pred - frac_pos)
        bins.append({
            "bin_index": i,
            "mean_predicted": round(mean_pred, 4),
            "fraction_positive": round(frac_pos, 4),
            "count": count,
        })

    return {
        "bins": bins,
        "ece": round(ece, 6),
        "brier": round(compute_brier_score(predictions, outcomes), 6),
    }


# ---------------------------------------------------------------------------
# Base-rate helper
# ---------------------------------------------------------------------------

def compute_base_rate(city: str, threshold: float, month: int,
                      db_path: str) -> float:
    """Return the historical fraction of days where temperature exceeded *threshold*.

    Expected schema for the ``history.db`` database::

        CREATE TABLE IF NOT EXISTS temperature (
            city    TEXT,
            month   INTEGER,
            tmax    REAL
        );

    Args:
        city:      City name (must match DB rows).
        threshold: Temperature threshold (°C).
        month:     Month number (1-12).
        db_path:   Path to the SQLite database.

    Returns:
        Fraction of matching days where tmax > threshold.
    """
    db = Path(db_path)
    if not db.exists():
        logger.warning("history.db not found at %s — returning 0.0", db_path)
        return 0.0

    try:
        conn = sqlite3.connect(str(db))
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM temperature WHERE city = ? AND month = ?",
            (city, month),
        )
        total = cur.fetchone()[0]
        if total == 0:
            logger.warning("No temperature rows for city=%s month=%d", city, month)
            conn.close()
            return 0.0

        cur.execute(
            "SELECT COUNT(*) FROM temperature WHERE city = ? AND month = ? AND tmax > ?",
            (city, month, threshold),
        )
        hits = cur.fetchone()[0]
        conn.close()
        return hits / total

    except sqlite3.Error as exc:
        logger.error("SQLite error computing base rate: %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# ProbabilityCalibrator — persistent, per-city calibration store
# ---------------------------------------------------------------------------

class ProbabilityCalibrator:
    """Persistent calibration manager with per-city Platt params.

    On construction the calibrator loads previously-fitted parameters from
    *storage_path* (JSON).  Call :meth:`update_calibration` periodically
    (e.g. weekly) to refit parameters from settled trades, then
    :meth:`calibrate_probability` applies the stored mapping for live scoring.

    The on-disk schema::

        {
            "platt": {
                "global": {"a": 1.0, "b": 0.0},
                "cities": {
                    "London": {"a": 0.9, "b": 0.1},
                    ...
                }
            }
        }
    """

    STORAGE_DEFAULT = "/polybot-data/calibration.json"

    def __init__(self, storage_path: str = STORAGE_DEFAULT) -> None:
        self.storage_path = Path(storage_path)
        self.platt_global: dict = {"a": 1.0, "b": 0.0}
        self.platt_cities: dict[str, dict] = {}
        self._load_from_disk()
        logger.info(
            "ProbabilityCalibrator loaded from %s (%d city profiles)",
            self.storage_path,
            len(self.platt_cities),
        )

    # -- persistence -------------------------------------------------------

    def _load_from_disk(self) -> None:
        if not self.storage_path.exists():
            logger.info("No calibration file at %s — using defaults", self.storage_path)
            return
        try:
            data = json.loads(self.storage_path.read_text())
            self.platt_global = data.get("platt", {}).get("global", {"a": 1.0, "b": 0.0})
            self.platt_cities = data.get("platt", {}).get("cities", {})
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Failed to read calibration file: %s — using defaults", exc)

    def _save_to_disk(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "platt": {
                "global": self.platt_global,
                "cities": self.platt_cities,
            }
        }
        self.storage_path.write_text(json.dumps(payload, indent=2))
        logger.info("Calibration saved to %s", self.storage_path)

    # -- calibration update ------------------------------------------------

    async def update_calibration(self, trade_log_path: str) -> dict:
        """Re-fit Platt params from a settled-trade JSONL log.

        Expected JSONL schema (one object per line)::

            {"city": "London", "predicted_probability": 0.75, "settled": true, "outcome": 1}

        Only ``settled`` trades contribute to fitting.  The parameters *a*
        and *b* are estimated via a single-step Newton update on the
        negative log-likelihood of the Bernoulli model (simplified Platt
        fitting).

        Args:
            trade_log_path: Path to the JSONL trade log.

        Returns:
            Summary dict with keys: city, n_trades, calib_error, platt_a, platt_b.
        """
        raw_probs: list[float] = []
        outcomes_list: list[int] = []
        city = "default"

        log_file = Path(trade_log_path)
        if not log_file.exists():
            logger.warning("Trade log not found: %s", trade_log_path)
            return {"error": "log_not_found", "path": trade_log_path}

        # Read settled trades
        with log_file.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not record.get("settled"):
                    continue
                raw_probs.append(record["predicted_probability"])
                outcomes_list.append(record["outcome"])
                city = record.get("city", city)

        n = len(raw_probs)
        if n < 5:
            logger.warning("Only %d settled trades — skipping recalibration", n)
            return {"skipped": True, "reason": "too_few_trades", "count": n}

        # Fit via simple Platt-style logistic regression (Newton steps)
        a, b = 1.0, 0.0
        for _ in range(25):
            grad_a = 0.0
            grad_b = 0.0
            hess_a = 0.0
            hess_b = 0.0
            hess_ab = 0.0
            for p, o in zip(raw_probs, outcomes_list):
                z = math.exp(-a * p - b)
                q = 1.0 / (1.0 + z)  # calibrated prediction
                d = q - o
                grad_a += d * p
                grad_b += d * 1.0
                w = q * (1.0 - q)
                hess_a += w * p * p
                hess_b += w * 1.0
                hess_ab += w * p

            det = hess_a * hess_b - hess_ab * hess_ab
            if abs(det) < 1e-12:
                break

            # Inverse Hessian * gradient (Newton step)
            inv_h_a = hess_b / det
            inv_h_b = hess_a / det
            inv_h_ab = -hess_ab / det

            a -= inv_h_a * grad_a + inv_h_ab * grad_b
            b -= inv_h_ab * grad_a + inv_h_b * grad_b
            # Clamp to avoid divergence
            a = max(0.05, min(a, 20.0))
            b = max(-50.0, min(b, 50.0))

        self.platt_cities[city] = {"a": round(a, 6), "b": round(b, 6)}

        # If no per-city key matched, update global too
        if city == "default":
            self.platt_global = {"a": round(a, 6), "b": round(b, 6)}

        self._save_to_disk()

        # Compute diagnostics post-fit
        calib = compute_calibration_error(raw_probs, outcomes_list)

        result = {
            "city": city,
            "n_trades": n,
            "platt_a": round(a, 6),
            "platt_b": round(b, 6),
            "ece": calib["ece"],
            "brier": calib["brier"],
        }
        logger.info("Calibration updated: %s", result)
        return result

    # -- apply calibration --------------------------------------------------

    def calibrate_probability(self, raw_prob: float,
                              city: str = "default") -> float:
        """Return a calibrated probability for *raw_prob* using stored params.

        Falls back to global params when no per-city entry exists.  The result
        is clamped to [0.02, 0.98] to avoid extreme certainties.

        Args:
            raw_prob: Raw model probability.
            city:     City key (default ``"default"`` for global params).

        Returns:
            Calibrated probability in [0.02, 0.98].
        """
        params = self.platt_cities.get(city, self.platt_global)
        a = params.get("a", 1.0)
        b = params.get("b", 0.0)
        calibrated = platt_scale(raw_prob, a, b)
        return max(0.02, min(0.98, calibrated))

    # -- diagnostics -------------------------------------------------------

    def get_params(self, city: str = "global") -> dict:
        """Return the stored Platt parameters for *city*."""
        if city == "global":
            return dict(self.platt_global)
        return dict(self.platt_cities.get(city, self.platt_global))

    def list_cities(self) -> list[str]:
        """Return list of cities with per-city calibration profiles."""
        return list(self.platt_cities.keys())
