"""
Ladder strategy allocation and GFS timing windows for Polymarket weather trading.

This module handles:
  - GFS run timing windows: identifies when fresh GFS forecast data is available
    and the optimal trading window after each run.
  - Ladder allocation: distributes bankroll across the top-N bucket edges using
    a fixed weight scheme (60/25/15) with per-bet caps.
  - Tail bucket filtering: identifies high-multiplier tail bets from low-price
    buckets where significant edge exists.
  - LadderState: persistent state tracking for active ladder positions per city,
    saved to JSON.
  - Trade logging: append-only JSONL log for ladder trades.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GFS timing constants
# ---------------------------------------------------------------------------

GFS_RUN_TIMES = [0, 6, 12, 18]  # UTC hours when GFS model runs
GFS_WINDOW_MINUTES = 10  # Trade window duration after each GFS run (minutes)

# Trade window starts this many minutes *after* the top-of-hour GFS run.
# GFS data typically becomes available ~4-5 hours after the initialization
# time, but for traded weather markets the relevant info leak / market
# adjustment begins ~5 minutes after the run hour.
GFS_WINDOW_START_OFFSET = 5  # minutes after GFS run hour

# Ladder allocation constants
_LADDER_TOP_N = 3
_LADDER_WEIGHTS = [0.60, 0.25, 0.15]  # must sum to ~1.0
_MAX_SINGLE_BET_USD = 0.30
_BANKROLL_FRACTION_PER_BET = 0.025  # 2.5 % of bankroll per bet


# ---------------------------------------------------------------------------
# GFS window helpers
# ---------------------------------------------------------------------------

def is_gfs_window(now: datetime | None = None) -> bool:
    """Return *True* if *now* falls within a GFS post-run trading window.

    Each GFS run produces fresh forecast data that can move Polymarket
    weather markets.  The window opens ``GFS_WINDOW_START_OFFSET`` minutes
    after each GFS run hour and lasts ``GFS_WINDOW_MINUTES`` minutes.

    Parameters
    ----------
        now : datetime, optional
            UTC datetime to evaluate.  Defaults to ``datetime.now(timezone.utc)``.

    Window examples (with defaults):
        00:05 - 00:15 UTC
        06:05 - 06:15 UTC
        12:05 - 12:15 UTC
        18:05 - 18:15 UTC
    """
    if now is None:
        now = datetime.now(timezone.utc)

    current_minute = now.minute
    hour = now.hour

    if hour not in GFS_RUN_TIMES:
        return False

    window_start = GFS_WINDOW_START_OFFSET
    window_end = GFS_WINDOW_START_OFFSET + GFS_WINDOW_MINUTES
    return window_start <= current_minute < window_end


def next_gfs_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the *(start, end)* datetimes for the ``**next**`` GFS window.

    Useful for scheduling: sleep until the returned start time.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Generate candidate window starts from today's run times
    today = now.date()
    candidates: list[datetime] = []
    for h in GFS_RUN_TIMES:
        ws = datetime(today.year, today.month, today.day, h, GFS_WINDOW_START_OFFSET, tzinfo=timezone.utc)
        candidates.append(ws)

    # Also include tomorrow's first run so we always have a future candidate
    tomorrow = today + timedelta(days=1)
    ws_first = datetime(tomorrow.year, tomorrow.month, tomorrow.day, GFS_RUN_TIMES[0], GFS_WINDOW_START_OFFSET, tzinfo=timezone.utc)
    candidates.append(ws_first)

    window_duration = timedelta(minutes=GFS_WINDOW_MINUTES)
    for ws in candidates:
        we = ws + window_duration
        if ws > now:
            return ws, we

    # Fallback (should never happen with tomorrow candidate)
    ws = datetime(today.year, today.month, today.day, GFS_RUN_TIMES[0], GFS_WINDOW_START_OFFSET, tzinfo=timezone.utc)
    return ws, ws + window_duration


# ---------------------------------------------------------------------------
# Ladder allocation
# ---------------------------------------------------------------------------

def compute_ladder_allocation(
    edge_data: list[dict[str, Any]],
    bankroll: float,
) -> list[dict[str, Any]]:
    """Allocate bankroll across the top-3 edge opportunities.

    Each selected bucket receives a fixed-percentage weight of the bankroll,
    capped at ``_MAX_SINGLE_BET_USD``.  The weights (60 / 25 / 15) are
    applied to a base stake of ``bankroll * 0.025``.

    Parameters
    ----------
        edge_data : list[dict]
            Each dict must contain at least ``bucket``, ``prob``, ``price``,
            and ``edge`` keys.
        bankroll : float
            Total available capital in USD.

    Returns
    -------
        list[dict]  – one entry per allocated position with keys:
            bucket, weight, amount_usd, prob, price, edge
    """
    if not edge_data or bankroll <= 0:
        return []

    sorted_data = sorted(edge_data, key=lambda d: d.get("edge", 0.0), reverse=True)
    top = sorted_data[:_LADDER_TOP_N]

    base_stake = bankroll * _BANKROLL_FRACTION_PER_BET
    allocations: list[dict[str, Any]] = []

    for item, weight in zip(top, _LADDER_WEIGHTS):
        raw_amount = base_stake * weight
        amount = min(raw_amount, _MAX_SINGLE_BET_USD)
        allocations.append({
            "bucket": item["bucket"],
            "weight": weight,
            "amount_usd": round(amount, 4),
            "prob": item["prob"],
            "price": item["price"],
            "edge": item["edge"],
        })

    logger.debug("Ladder allocation: %s", allocations)
    return allocations


# ---------------------------------------------------------------------------
# Tail bucket filtering
# ---------------------------------------------------------------------------

def get_tail_buckets(
    markets: list[dict[str, Any]],
    max_price: float = 0.10,
    min_prob: float = 0.15,
) -> list[dict[str, Any]]:
    """Return high-multiplier tail bucket markets filtered by price and probability.

    Tail bets have a low market price (implying low implied probability) but
    our model assigns a relatively high true probability — producing a large
    multiplier if correct.

    Parameters
    ----------
        markets : list[dict]
            Each dict must contain ``price``, ``prob`` and ideally ``edge``.
        max_price : float
            Maximum market price (default 0.10 = 10 cents).
        min_prob : float
            Minimum model probability (default 0.15 = 15 %).

    Returns
    -------
        list[dict]  – filtered and edge-descending sorted markets.
    """
    filtered = [
        m for m in markets
        if m.get("price", 1.0) < max_price and m.get("prob", 0.0) > min_prob
    ]
    filtered.sort(key=lambda d: d.get("edge", 0.0), reverse=True)
    logger.debug("Tail buckets found: %d (from %d markets)", len(filtered), len(markets))
    return filtered


# ---------------------------------------------------------------------------
# Persistent ladder state
# ---------------------------------------------------------------------------

class LadderState:
    """Persistent per-city ladder position tracker backed by a JSON file.

    Each city key maps to a list of active position dicts.  Positions are
    identified by their ``bucket`` value within a city.

    Parameters
    ----------
        state_path : str
            Path to the JSON state file.
    """

    def __init__(self, state_path: str = "/polybot-data/ladder_state.json") -> None:
        self.state_path = Path(state_path)
        self._data: dict[str, list[dict[str, Any]]] = {}
        self.load()

    # -- mutation helpers ---------------------------------------------------

    def add_position(self, city: str, position: dict[str, Any]) -> None:
        """Add (or replace) a ladder *position* for *city*."""
        if city not in self._data:
            self._data[city] = []
        # Replace existing position with the same bucket, if present
        bucket = position.get("bucket")
        self._data[city] = [p for p in self._data[city] if p.get("bucket") != bucket]
        self._data[city].append(position)
        self.save()

    def remove_position(self, city: str, bucket: Any) -> None:
        """Remove a position by *bucket* from *city*."""
        if city in self._data:
            self._data[city] = [p for p in self._data[city] if p.get("bucket") != bucket]
            self.save()

    # -- read helpers -------------------------------------------------------

    def get_positions(self, city: str) -> list[dict[str, Any]]:
        """Return active positions for *city* (empty list if none)."""
        return list(self._data.get(city, []))

    def get_all(self) -> dict[str, list[dict[str, Any]]]:
        """Return a shallow copy of the full state dict."""
        return {k: list(v) for k, v in self._data.items()}

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        """Write state to *state_path* as formatted JSON."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as fh:
            json.dump(self._data, fh, indent=2, default=str)
        logger.debug("LadderState saved to %s", self.state_path)

    def load(self) -> None:
        """Load state from *state_path*.  Missing file is not an error."""
        if self.state_path.exists():
            try:
                with open(self.state_path) as fh:
                    self._data = json.load(fh)
                logger.debug("LadderState loaded from %s (%d cities)", self.state_path, len(self._data))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load ladder state: %s — starting fresh", exc)
                self._data = {}
        else:
            self._data = {}

    def __repr__(self) -> str:
        return f"<LadderState path={self.state_path} cities={list(self._data.keys())}>"


# ---------------------------------------------------------------------------
# Trade logging
# ---------------------------------------------------------------------------

def log_ladder_trade(
    trade: dict[str, Any],
    log_path: str = "/polybot-data/ladder_log.jsonl",
) -> None:
    """Append *trade* as a JSON line to *log_path*.

    A ``timestamp_utc`` field is injected automatically if not already
    present in *trade*.

    Parameters
    ----------
        trade : dict
            Trade record to log.
        log_path : str
            Path to the append-only JSONL log file.
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    record = dict(trade)  # shallow copy to avoid mutating caller's dict
    record.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())

    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        logger.debug("Ladder trade logged to %s", log_path)
    except OSError as exc:
        logger.error("Failed to log ladder trade: %s", exc)
