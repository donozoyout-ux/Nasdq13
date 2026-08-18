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


# ----------------------------------------------------------------------
# Candle Blending + Price Action (Mum Mantığı analizörü)
# Sistemi: karmaşık formasyon adları yerine gövde/fitil oranları, momentum
# yutan mumlar ve mum birleştirme (candle blending) ile "kim üstün" sorusuna
# kural tabanlı (AI'sız) cevap verir.
# ----------------------------------------------------------------------

def _blend_candles(df: pd.DataFrame, lookback: int = 3) -> Optional[Dict[str, float]]:
    """Candle Blending: son `lookback` mumu TEK mum olarak birleştirir.
    Açılış = ilk mumun açılışı, kapanış = son mumun kapanışı,
    yüksek = en yüksek high, düşük = en düşük low."""
    if df is None or len(df) < lookback:
        return None
    sub = df.tail(lookback)
    o = float(sub["open"].iloc[0])
    c = float(sub["close"].iloc[-1])
    h = float(sub["high"].max())
    l = float(sub["low"].min())
    return {"open": o, "close": c, "high": h, "low": l}


def _candle_anatomy(o: float, c: float, h: float, l: float) -> Dict[str, Any]:
    """Tek bir mumun (veya blended mumun) anatomik ölçümleri."""
    rng = _range(h, l)
    body = _body(o, c)
    upper = h - max(o, c)
    lower = min(o, c) - l
    bull = c >= o
    body_ratio = body / rng if rng > 0 else 0.0
    return {
        "bull": bull,
        "body_ratio": body_ratio,
        "upper_wick_ratio": upper / rng if rng > 0 else 0.0,
        "lower_wick_ratio": lower / rng if rng > 0 else 0.0,
        "upper_wick": upper,
        "lower_wick": lower,
        "body": body,
        "range": rng,
    }


def price_action_analysis(df: pd.DataFrame, lookback: int = 8) -> Dict[str, Any]:
    """Kural tabanlı 'Mum Mantığı + Candle Blending' analizi.

    Adım adım:
      1) Son mumun anatomisi (gövde vs fitiller) → alıcı/satıcı baskısı
      2) Önceki 1-2 mumun gövdesini yutan mum (engulfing) → momentum teyidi
      3) Son 3 mumu TEK mumda birleştir (candle blending) → nihai yön
      4) 3-5 mumluk kümülatif gövdeler: trend yönünde kim daha büyük hareket yapmış
      5) Nihai karar: alıcı mı satıcı mı üstün + yön beklentisi + açıklama

    Returns a dict: {bias, direction, verdict, steps, score}.
    """
    out = {
        "bias": 0,          # +1 alıcı üstün / -1 satıcı üstün / 0 nötr
        "direction": "nötr",
        "verdict": "",
        "steps": [],
        "score": 0.0,
    }
    if df is None or len(df) < 5:
        out["verdict"] = "Yeterli veri yok."
        return out

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)

    steps: List[str] = []
    score = 0.0

    # --- 1) Son mum anatomisi ---
    last = _candle_anatomy(o[-1], c[-1], h[-1], l[-1])
    if last["body_ratio"] > 0.55:
        if last["bull"]:
            score += 2
            steps.append(
                "Son mum: büyük yeşil gövdeli (gövde aralığın %{:.0f}'i) — alıcılar kontrolü elinde tutuyor.".format(
                    last["body_ratio"] * 100
                )
            )
        else:
            score -= 2
            steps.append(
                "Son mum: büyük kırmızı gövdeli (gövde aralığın %{:.0f}'i) — satıcılar baskın.".format(
                    last["body_ratio"] * 100
                )
            )
    elif last["body_ratio"] < 0.25:
        # Kararsızlık / fitil baskısı
        if last["lower_wick_ratio"] > 0.55:
            score += 1.5
            steps.append(
                "Son mum: uzun alt fitil (%{:.0f}) — düşük seviyeler reddedildi, alıcılar yukarı itti.".format(
                    last["lower_wick_ratio"] * 100
                )
            )
        elif last["upper_wick_ratio"] > 0.55:
            score -= 1.5
            steps.append(
                "Son mum: uzun üst fitil (%{:.0f}) — üst seviyeler reddedildi, satıcılar aşağı bastırdı.".format(
                    last["upper_wick_ratio"] * 100
                )
            )
        else:
            steps.append("Son mum: küçük gövdeli, kararsızlık (doji benzeri).")

    # --- 2) Engulfing momentum teyidi (son mum önceki 1-2 mumun gövdesini yutar) ---
    if n >= 2:
        prev = _candle_anatomy(o[-2], c[-2], h[-2], l[-2])
        if prev["body"] > 0 and last["body"] > prev["body"]:
            if last["bull"] and not prev["bull"]:
                score += 2.5
                steps.append(
                    "Son mum önceki kırmızı mumun gövdesini tamamen yutuyor (bullish engulfing) — güçlü yön değişimi teyidi."
                )
            elif not last["bull"] and prev["bull"]:
                score -= 2.5
                steps.append(
                    "Son mum önceki yeşil mumun gövdesini tamamen yutuyor (bearish engulfing) — güçlü satış teyidi."
                )
        elif n >= 3 and prev["body"] <= last["body"] * 0.5:
            steps.append("Son mum, önceki mumun gövdesini aşıyor ama tam yutma yok — momentum zayıf teyit.")

    # --- 3) Candle Blending: son 3 mumu tek mumda birleştir ---
    blend = _blend_candles(df, lookback=3)
    if blend:
        ba = _candle_anatomy(blend["open"], blend["close"], blend["high"], blend["low"])
        blend_pct = round((blend["close"] / blend["open"] - 1) * 100, 2) if blend["open"] else 0.0
        if ba["bull"] and ba["body_ratio"] > 0.5:
            score += 2
            steps.append(
                "Candle blending (son 3 mum): birleşik mum yeşil, gövde %{:.0f} — nihai eğilim yukarı ({}% net hareket).".format(
                    ba["body_ratio"] * 100, blend_pct
                )
            )
        elif not ba["bull"] and ba["body_ratio"] > 0.5:
            score -= 2
            steps.append(
                "Candle blending (son 3 mum): birleşik mum kırmızı, gövde %{:.0f} — nihai eğilim aşağı ({}% net hareket).".format(
                    ba["body_ratio"] * 100, blend_pct
                )
            )
        else:
            # Blended mum fitil baskın — hangi fitil?
            if ba["lower_wick_ratio"] > ba["upper_wick_ratio"]:
                score += 1
                steps.append(
                    "Candle blending (son 3 mum): birleşik mumda alt fitil uzun (%{:.0f}) — alıcılar alt bölgeyi savundu.".format(
                        ba["lower_wick_ratio"] * 100
                    )
                )
            else:
                score -= 1
                steps.append(
                    "Candle blending (son 3 mum): birleşik mumda üst fitil uzun (%{:.0f}) — satıcılar üst bölgeyi savundu.".format(
                        ba["upper_wick_ratio"] * 100
                    )
                )

    # --- 4) Son 5 mumun kümülatif gövde netliği ---
    seg = max(n - 5, 0)
    net_body = float(np.sum(np.abs(c[seg:] - o[seg:]) * np.sign(c[seg:] - o[seg:])))
    gross_body = float(np.sum(np.abs(c[seg:] - o[seg:])))
    if gross_body > 0:
        net_ratio = net_body / gross_body
        if net_ratio > 0.25:
            score += 1.5
            steps.append(
                "Son 5 mum: gövdelerin %{:.0f}'i yukarı yönde — toplu momentum alıcılarda.".format(net_ratio * 100)
            )
        elif net_ratio < -0.25:
            score -= 1.5
            steps.append(
                "Son 5 mum: gövdelerin %{:.0f}'i aşağı yönde — toplu momentum satıcılarda.".format(abs(net_ratio) * 100)
            )

    # --- 5) Nihai karar ---
    if score >= 3:
        bias, direction = 1, "yükseliş"
    elif score <= -3:
        bias, direction = -1, "düşüş"
    else:
        bias, direction = 0, "nötr"

    verdict = {
        1: "Alıcılar üstün — fiyat davranışı yükseliş yönünde. Yön beklentisi: YUKARI.",
        -1: "Satıcılar üstün — fiyat davranışı düşüş yönünde. Yön beklentisi: AŞAĞI.",
        0: "Alıcılar ve satıcılar dengede — kesin yön sinyali yok. Kırılım onayı beklenmeli.",
    }[bias]

    out.update({
        "bias": bias,
        "direction": direction,
        "verdict": verdict,
        "steps": steps,
        "score": round(score, 1),
    })
    return out
