"""
Pattern & Level Detection (rule-based, free)
- Candlestick patterns: hammer, shooting star, engulfing, doji, pin bar,
  NR7 (narrowest range), inside bar
- Support/Resistance via swing pivots + round numbers
- Session-based VWAP (intraday) — not the cumulative history VWAP

These feed the small-cap setup/trigger scores. No API key required.
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Any

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# Candlestick patterns
# ----------------------------------------------------------------------

def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return max(h - l, 1e-9)


def detect_candles(df: pd.DataFrame, lookback: int = 5) -> List[Dict[str, Any]]:
    """Detect candlestick patterns on the last `lookback` bars (oldest first).
    Returns a list of {bar, pattern, bullish, strength, note} dicts."""
    patterns: List[Dict[str, Any]] = []
    if df is None or len(df) < 3:
        return patterns

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)

    start = max(n - lookback, 1)
    for i in range(start, n):
        body = _body(o[i], c[i])
        rng = _range(h[i], l[i])
        upper_wick = h[i] - max(o[i], c[i])
        lower_wick = min(o[i], c[i]) - l[i]
        bull = c[i] >= o[i]
        found = False

        # Doji: tiny body relative to range
        if rng > 0 and body / rng < 0.08:
            patterns.append({"bar": i, "pattern": "doji", "bullish": 0,
                             "strength": 1, "note": "Kararsızlık mumu (doji)"})
            found = True

        # Hammer: small body in upper half + long lower wick
        if rng > 0 and body / rng < 0.35 and lower_wick > 2 * body and lower_wick > upper_wick * 2:
            if not found:
                patterns.append({"bar": i, "pattern": "hammer", "bullish": 1,
                                 "strength": 2, "note": "Çekiç (hammer) — alım baskısı"})
                found = True

        # Shooting star: small body in lower half + long upper wick
        if rng > 0 and body / rng < 0.35 and upper_wick > 2 * body and upper_wick > lower_wick * 2:
            if not found:
                patterns.append({"bar": i, "pattern": "shooting_star", "bullish": -1,
                                 "strength": 2, "note": "Kayan yıldız — satış baskısı"})
                found = True

        # Engulfing (compare with previous bar)
        if i >= 1:
            prev_o, prev_c = o[i - 1], c[i - 1]
            prev_bull = prev_c >= prev_o
            prev_body = _body(prev_o, prev_c)
            if prev_body > 0:
                if bull and not prev_bull and body > prev_body and o[i] <= prev_c and c[i] >= prev_o:
                    patterns.append({"bar": i, "pattern": "bullish_engulfing", "bullish": 1,
                                     "strength": 3, "note": "Boğa yutan mum (bullish engulfing)"})
                    found = True
                elif not bull and prev_bull and body > prev_body and o[i] >= prev_c and c[i] <= prev_o:
                    patterns.append({"bar": i, "pattern": "bearish_engulfing", "bullish": -1,
                                     "strength": 3, "note": "Ayı yutan mum (bearish engulfing)"})
                    found = True

        # Inside bar (NR-adjacent): current range fully inside previous range
        if i >= 1 and not found:
            if h[i] <= h[i - 1] and l[i] >= l[i - 1]:
                patterns.append({"bar": i, "pattern": "inside_bar", "bullish": 0,
                                 "strength": 1, "note": "İç bar (inside bar) — sıkışma"})
                found = True

        # Pin bar / strong rejection with long single wick
        if not found and rng > 0:
            wick_ratio = max(upper_wick, lower_wick) / rng
            if wick_ratio > 0.6 and body / rng < 0.4:
                bull_pin = lower_wick > upper_wick
                patterns.append({"bar": i, "pattern": "pin_bar", "bullish": 1 if bull_pin else -1,
                                 "strength": 2,
                                 "note": "Pin bar (uzun fitil)" + (" — alım" if bull_pin else " — satış")})
                found = True

    return patterns


def is_nr7(df: pd.DataFrame) -> bool:
    """NR7: last bar has the narrowest range of the prior 7 bars."""
    if df is None or len(df) < 8:
        return False
    ranges = (df["high"] - df["low"]).values
    last = ranges[-1]
    return last <= ranges[-8:-1].min() and last > 0


# ----------------------------------------------------------------------
# Support / Resistance via swing pivots
# ----------------------------------------------------------------------

def find_pivots(df: pd.DataFrame, window: int = 5) -> Tuple[List[float], List[float]]:
    """Detect swing-high (resistance) and swing-low (support) pivot levels.
    window = bars to the left/right used to confirm a pivot."""
    if df is None or len(df) < window * 2 + 2:
        return [], []
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    resistance: List[float] = []
    support: List[float] = []
    for i in range(window, n - window):
        left_h = highs[i - window:i]
        right_h = highs[i + 1:i + window + 1]
        left_l = lows[i - window:i]
        right_l = lows[i + 1:i + window + 1]
        if len(left_h) == window and len(right_h) == window:
            if highs[i] > left_h.max() and highs[i] >= right_h.max():
                resistance.append(float(highs[i]))
            if lows[i] < left_l.min() and lows[i] <= right_l.min():
                support.append(float(lows[i]))
    return resistance, support


def nearest_levels(df: pd.DataFrame, price: float, window: int = 5,
                   lookback: int = 120) -> Dict[str, float]:
    """Nearest resistance (above) and support (below) to `price` using swing
    pivots, rounded, plus round-number levels. Returns distances in %."""
    out = {
        "resistance": 0.0,
        "support": 0.0,
        "dist_res_pct": 0.0,
        "dist_sup_pct": 0.0,
        "res_type": "",
        "sup_type": "",
    }
    if df is None or len(df) < 3 or price <= 0:
        return out

    sub = df.tail(lookback).copy()
    resist, support = find_pivots(sub, window=window)

    # Round-number levels (e.g. 10, 15, 20, 50...) for mid-cap names
    tick = 1.0 if price < 100 else 5.0
    round_up = (np.floor(price / tick) + 1) * tick
    round_down = np.floor(price / tick) * tick

    cand_res = [lv for lv in resist if lv > price]
    cand_sup = [lv for lv in support if lv < price]
    cand_res.append(round_up)
    cand_sup.append(round_down)

    if cand_res:
        r = min(cand_res)
        out["resistance"] = r
        out["dist_res_pct"] = (r / price - 1) * 100
        out["res_type"] = "pivot" if r != round_up else "round"
    if cand_sup:
        s = max(cand_sup)
        out["support"] = s
        out["dist_sup_pct"] = (s / price - 1) * 100
        out["sup_type"] = "pivot" if s != round_down else "round"
    return out


# ----------------------------------------------------------------------
# Session VWAP (intraday) — resets each trading day
# ----------------------------------------------------------------------

def session_vwap(df: pd.DataFrame) -> float:
    """Real intraday VWAP for the current session (uses datetime index to split
    by day). Returns the latest cumulative session VWAP. Falls back to the
    classic cumulative VWAP if no timezone-aware daily index exists."""
    if df is None or len(df) < 2:
        return 0.0
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan).fillna(0)
    try:
        idx = df.index
        if isinstance(idx, pd.DatetimeIndex) and len(idx) > 0:
            dates = idx.normalize()
            if dates.nunique() > 1:
                cum_typ = (tp * vol).groupby(dates).cumsum()
                cum_vol = vol.groupby(dates).cumsum()
                vwap = (cum_typ / cum_vol.replace(0, np.nan))
                last = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else 0.0
                return last
    except Exception as e:
        logger.debug(f"Session VWAP fallback: {e}")
    return float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1]) if vol.cumsum().iloc[-1] > 0 else 0.0
