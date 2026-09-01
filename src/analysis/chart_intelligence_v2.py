"""Deep deterministic chart intelligence for the Railway dashboard.

This module is intentionally analysis-only. It does not mutate the live scanner,
ranking weights, thresholds, journal, or backtest strategy. It reads completed
OHLCV bars and exposes explainable chart diagnostics for manual inspection.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.analysis.chart_analysis import ChartAnalysisService


class ChartIntelligenceV2Service:
    """Advanced single-timeframe price-action and trade-location reader."""

    def __init__(self) -> None:
        self.reader = ChartAnalysisService(cache_ttl_seconds=60)

    async def analyze(self, symbol: str, timeframe: str = "15m") -> Dict[str, Any]:
        clean = self.reader._normalize_symbol(symbol)
        tf = (timeframe or "15m").lower().strip()
        if tf not in self.reader.TIMEFRAMES:
            raise ValueError("timeframe must be one of: 15m, 1h, 1d")
        df = await self.reader._fetch(clean, tf)
        return self._analyze_df(clean, tf, df)

    @staticmethod
    def _safe(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            v = float(value)
            return default if math.isnan(v) or math.isinf(v) else v
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - (100 / (1 + rs))).fillna(50.0)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        a = df["high"] - df["low"]
        b = (df["high"] - df["close"].shift()).abs()
        c = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([a, b, c], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()

    @staticmethod
    def _vwap(df: pd.DataFrame, timeframe: str) -> pd.Series:
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        volume = df["volume"].fillna(0.0)
        if timeframe == "1d":
            num = (typical * volume).rolling(20, min_periods=1).sum()
            den = volume.rolling(20, min_periods=1).sum().replace(0, np.nan)
            return (num / den).fillna(df["close"])
        session = pd.Series([str(ts.date()) for ts in df.index], index=df.index)
        num = (typical * volume).groupby(session).cumsum()
        den = volume.groupby(session).cumsum().replace(0, np.nan)
        return (num / den).fillna(df["close"])

    @staticmethod
    def _pivots(df: pd.DataFrame, window: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
        highs: List[Tuple[int, float]] = []
        lows: List[Tuple[int, float]] = []
        for i in range(window, len(df) - window):
            hi = float(df["high"].iloc[i])
            lo = float(df["low"].iloc[i])
            hs = df["high"].iloc[i-window:i+window+1]
            ls = df["low"].iloc[i-window:i+window+1]
            if hi >= float(hs.max()):
                highs.append((i, hi))
            if lo <= float(ls.min()):
                lows.append((i, lo))
        return highs, lows

    @staticmethod
    def _percentile_rank(series: pd.Series, value: float, lookback: int = 120) -> float:
        s = series.dropna().tail(lookback)
        if s.empty:
            return 50.0
        return float((s <= value).mean() * 100.0)

    @staticmethod
    def _clamp(value: float) -> float:
        return round(float(np.clip(value, 0, 100)), 1)

    def _candle_read(self, df: pd.DataFrame, atr: float) -> Dict[str, Any]:
        row = df.iloc[-1]
        prev = df.iloc[-2]
        o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
        po, ph, pl, pc = map(float, (prev["open"], prev["high"], prev["low"], prev["close"]))
        rng = max(h - l, 1e-12)
        body = abs(c - o)
        upper = h - max(o, c)
        lower = min(o, c) - l
        body_pct = body / rng
        close_loc = (c - l) / rng
        patterns: List[str] = []
        bullish = c > o
        bearish = c < o

        if body_pct <= 0.12:
            patterns.append("DOJI")
        if bullish and body_pct >= 0.60 and close_loc >= 0.78:
            patterns.append("STRONG_BULLISH_CANDLE")
        if bearish and body_pct >= 0.60 and close_loc <= 0.22:
            patterns.append("STRONG_BEARISH_CANDLE")
        if bullish and o <= pc and c >= po and pc < po:
            patterns.append("BULLISH_ENGULFING")
        if bearish and o >= pc and c <= po and pc > po:
            patterns.append("BEARISH_ENGULFING")
        if lower >= max(body * 2.0, rng * 0.45) and upper <= rng * 0.20 and close_loc >= 0.60:
            patterns.append("HAMMER_REJECTION")
        if upper >= max(body * 2.0, rng * 0.45) and lower <= rng * 0.20 and close_loc <= 0.40:
            patterns.append("SHOOTING_STAR_REJECTION")
        if h < ph and l > pl:
            patterns.append("INSIDE_BAR")
        if h > ph and l < pl:
            patterns.append("OUTSIDE_BAR")

        atr_ratio = rng / max(atr, 1e-12)
        if atr_ratio >= 1.8 and body_pct >= 0.55:
            patterns.append("MOMENTUM_EXPANSION_CANDLE")
        if atr_ratio >= 2.2 and ((bullish and close_loc < 0.65) or (bearish and close_loc > 0.35)):
            patterns.append("EXHAUSTION_RISK")

        quality = 50.0
        quality += min(body_pct * 35.0, 25.0)
        quality += 12.0 if (bullish and close_loc >= 0.75) else 0.0
        quality -= 12.0 if upper / rng >= 0.45 else 0.0
        quality += 8.0 if lower / rng >= 0.35 and bullish else 0.0
        quality -= 10.0 if "DOJI" in patterns else 0.0
        quality -= 8.0 if "EXHAUSTION_RISK" in patterns else 0.0

        return {
            "patterns": patterns,
            "body_pct": round(body_pct * 100, 1),
            "close_location_pct": round(close_loc * 100, 1),
            "upper_wick_pct": round(upper / rng * 100, 1),
            "lower_wick_pct": round(lower / rng * 100, 1),
            "range_atr": round(atr_ratio, 2),
            "quality_score": self._clamp(quality),
        }

    def _analyze_df(self, symbol: str, timeframe: str, df: pd.DataFrame) -> Dict[str, Any]:
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].fillna(0.0).astype(float)

        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        rsi = self._rsi(close)
        atrs = self._atr(df)
        vwap = self._vwap(df, timeframe)
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        macd_hist = macd - macd_signal
        vol_sma = volume.rolling(20).mean()
        rvol = volume / vol_sma.replace(0, np.nan)
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + 2.0 * bb_std
        bb_lower = bb_mid - 2.0 * bb_std
        bb_width = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan) * 100.0
        prior_high20 = high.shift(1).rolling(20).max()
        prior_low20 = low.shift(1).rolling(20).min()

        price = float(close.iloc[-1])
        prev_price = float(close.iloc[-2])
        atr = self._safe(atrs.iloc[-1], max(price * 0.02, 0.01)) or max(price * 0.02, 0.01)
        rv = self._safe(rvol.iloc[-1], 0.0) or 0.0
        last_rsi = self._safe(rsi.iloc[-1], 50.0) or 50.0
        last_macd_hist = self._safe(macd_hist.iloc[-1], 0.0) or 0.0
        last_vwap = self._safe(vwap.iloc[-1], price) or price
        e9, e21, e50, e200 = [float(x.iloc[-1]) for x in (ema9, ema21, ema50, ema200)]

        piv_hi, piv_lo = self._pivots(df, window=3)
        recent_hi = piv_hi[-4:]
        recent_lo = piv_lo[-4:]
        last_swing_high = recent_hi[-1][1] if recent_hi else self._safe(prior_high20.iloc[-1])
        last_swing_low = recent_lo[-1][1] if recent_lo else self._safe(prior_low20.iloc[-1])

        structure = "MIXED"
        if len(recent_hi) >= 2 and len(recent_lo) >= 2:
            if recent_hi[-1][1] > recent_hi[-2][1] and recent_lo[-1][1] > recent_lo[-2][1]:
                structure = "HH_HL"
            elif recent_hi[-1][1] < recent_hi[-2][1] and recent_lo[-1][1] < recent_lo[-2][1]:
                structure = "LH_LL"

        prior_swing_high = recent_hi[-1][1] if recent_hi else self._safe(prior_high20.iloc[-1])
        prior_swing_low = recent_lo[-1][1] if recent_lo else self._safe(prior_low20.iloc[-1])
        bullish_bos = bool(prior_swing_high and price > prior_swing_high and prev_price <= prior_swing_high)
        bearish_bos = bool(prior_swing_low and price < prior_swing_low and prev_price >= prior_swing_low)
        choch = "NONE"
        if structure == "LH_LL" and bullish_bos:
            choch = "BULLISH_CHOCH"
        elif structure == "HH_HL" and bearish_bos:
            choch = "BEARISH_CHOCH"

        if price > e9 > e21 > e50 and e21 > ema21.iloc[-5]:
            trend = "STRONG_BULLISH"
        elif price > e21 > e50:
            trend = "BULLISH"
        elif price < e9 < e21 < e50 and e21 < ema21.iloc[-5]:
            trend = "BEARISH"
        else:
            trend = "TRANSITION"

        breakout_state = "NONE"
        breakout_level: Optional[float] = None
        breakout_age: Optional[int] = None
        for age in range(0, min(6, len(df) - 21)):
            idx = len(df) - 1 - age
            level = self._safe(prior_high20.iloc[idx])
            if level and float(close.iloc[idx]) > level:
                breakout_level = level
                breakout_age = age
                break
        tolerance = max(atr * 0.35, price * 0.0025)
        if breakout_level is not None and breakout_age is not None:
            if price < breakout_level - tolerance * 0.25:
                breakout_state = "FALSE_BREAKOUT" if breakout_age <= 3 else "FAILED_BREAKOUT"
            elif breakout_age == 0 and rv >= 1.2:
                breakout_state = "BREAKOUT_CONFIRMED"
            else:
                since_idx = len(df) - 1 - breakout_age
                lows_after = low.iloc[since_idx:].astype(float)
                touched = bool((lows_after <= breakout_level + tolerance).any())
                held = price >= breakout_level - tolerance * 0.15
                breakout_state = "BREAKOUT_RETEST_HOLD" if touched and held else "BREAKOUT_HOLDING"
        else:
            p20 = self._safe(prior_high20.iloc[-1])
            if p20 and 0 <= p20 - price <= max(atr, price * 0.012):
                breakout_state = "BREAKOUT_READY"
                breakout_level = p20

        candle = self._candle_read(df, atr)

        divergence = "NONE"
        if len(recent_lo) >= 2:
            (i1, p1), (i2, p2) = recent_lo[-2], recent_lo[-1]
            if p2 < p1 and float(rsi.iloc[i2]) > float(rsi.iloc[i1]) + 2.0:
                divergence = "BULLISH_RSI_DIVERGENCE"
        if len(recent_hi) >= 2:
            (i1, p1), (i2, p2) = recent_hi[-2], recent_hi[-1]
            if p2 > p1 and float(rsi.iloc[i2]) < float(rsi.iloc[i1]) - 2.0:
                divergence = "BEARISH_RSI_DIVERGENCE"

        supports = sorted([p for _, p in piv_lo[-25:] if p < price], reverse=True)
        resistances = sorted([p for _, p in piv_hi[-25:] if p > price])
        support = supports[0] if supports else self._safe(prior_low20.iloc[-1])
        resistance = resistances[0] if resistances else self._safe(prior_high20.iloc[-1])

        last_bbw = self._safe(bb_width.iloc[-1], 0.0) or 0.0
        bbw_pctile = self._percentile_rank(bb_width, last_bbw)
        atr_pct = atr / max(price, 1e-12) * 100.0
        atr_pct_series = atrs / close.replace(0, np.nan) * 100.0
        atr_percentile = self._percentile_rank(atr_pct_series, atr_pct)
        squeeze = bbw_pctile <= 20 and atr_percentile <= 35
        expansion = bbw_pctile >= 75 and atr_percentile >= 65

        recent_vol = float(volume.tail(5).mean())
        prior_vol = float(volume.iloc[-15:-5].mean()) if len(volume) >= 15 else recent_vol
        volume_trend = "RISING" if recent_vol > prior_vol * 1.15 else ("FALLING" if recent_vol < prior_vol * 0.85 else "FLAT")
        breakout_volume_confirmed = breakout_state in {"BREAKOUT_CONFIRMED", "BREAKOUT_RETEST_HOLD", "BREAKOUT_HOLDING"} and rv >= 1.2

        ema21_distance_atr = (price - e21) / max(atr, 1e-12)
        vwap_distance_atr = (price - last_vwap) / max(atr, 1e-12)
        support_distance_atr = ((price - support) / atr) if support else None
        resistance_distance_atr = ((resistance - price) / atr) if resistance else None
        chase = ema21_distance_atr > 2.2 or (resistance_distance_atr is not None and resistance_distance_atr < 0.45 and breakout_state not in {"BREAKOUT_CONFIRMED", "BREAKOUT_RETEST_HOLD"})

        structural_stop = (support - atr * 0.25) if support else (price - atr * 1.5)
        atr_stop = price - atr * 1.5
        stop = max(0.01, min(price - 0.01, max(structural_stop, atr_stop)))
        risk = max(price - stop, atr * 0.5, price * 0.003)
        room_r = ((resistance - price) / risk) if resistance and risk > 0 else None
        location = "GOOD_LOCATION"
        if chase:
            location = "CHASE_RISK"
        elif room_r is not None and room_r < 1.2:
            location = "POOR_RR_LOCATION"
        elif support_distance_atr is not None and support_distance_atr <= 1.0 and trend != "BEARISH":
            location = "NEAR_SUPPORT"
        elif breakout_state == "BREAKOUT_RETEST_HOLD":
            location = "RETEST_ENTRY_ZONE"

        target1 = price + risk * 2.0
        target2 = price + risk * 3.0

        trend_score = 50.0 + {"STRONG_BULLISH": 32, "BULLISH": 20, "TRANSITION": 0, "BEARISH": -32}[trend]
        trend_score += 8 if price > last_vwap else -8
        structure_score = 50.0 + (25 if structure == "HH_HL" else (-25 if structure == "LH_LL" else 0))
        structure_score += 15 if bullish_bos else (-15 if bearish_bos else 0)
        structure_score += 10 if choch == "BULLISH_CHOCH" else (-10 if choch == "BEARISH_CHOCH" else 0)
        breakout_score = 50.0 + {
            "BREAKOUT_CONFIRMED": 35,
            "BREAKOUT_RETEST_HOLD": 32,
            "BREAKOUT_HOLDING": 18,
            "BREAKOUT_READY": 10,
            "FALSE_BREAKOUT": -35,
            "FAILED_BREAKOUT": -30,
            "NONE": 0,
        }.get(breakout_state, 0)
        volume_score = 50.0 + (22 if rv >= 1.5 else 12 if rv >= 1.2 else -12 if rv < 0.7 else 0)
        volume_score += 8 if volume_trend == "RISING" else (-6 if volume_trend == "FALLING" else 0)
        momentum_score = 50.0
        momentum_score += 18 if 52 <= last_rsi <= 68 else (-12 if last_rsi < 42 else -8 if last_rsi > 76 else 0)
        momentum_score += 12 if last_macd_hist > 0 else -10
        momentum_score += 14 if divergence == "BULLISH_RSI_DIVERGENCE" else (-14 if divergence == "BEARISH_RSI_DIVERGENCE" else 0)
        volatility_score = 62.0 if squeeze else (58.0 if expansion and trend in {"STRONG_BULLISH", "BULLISH"} else 50.0)
        location_score = 72.0 if location in {"NEAR_SUPPORT", "RETEST_ENTRY_ZONE", "GOOD_LOCATION"} else 35.0
        if room_r is not None:
            location_score += 12 if room_r >= 2.0 else (-18 if room_r < 1.2 else 0)
        candle_score = float(candle["quality_score"])

        components = {
            "trend": self._clamp(trend_score),
            "structure": self._clamp(structure_score),
            "breakout": self._clamp(breakout_score),
            "volume": self._clamp(volume_score),
            "momentum": self._clamp(momentum_score),
            "candle_quality": self._clamp(candle_score),
            "volatility": self._clamp(volatility_score),
            "trade_location": self._clamp(location_score),
        }
        weights = {
            "trend": 0.16,
            "structure": 0.18,
            "breakout": 0.16,
            "volume": 0.12,
            "momentum": 0.10,
            "candle_quality": 0.10,
            "volatility": 0.06,
            "trade_location": 0.12,
        }
        overall = sum(components[k] * weights[k] for k in weights)
        if breakout_state in {"FALSE_BREAKOUT", "FAILED_BREAKOUT"}:
            overall -= 15
        if chase:
            overall -= 10
        if divergence == "BEARISH_RSI_DIVERGENCE":
            overall -= 7
        overall = self._clamp(overall)

        if breakout_state in {"FALSE_BREAKOUT", "FAILED_BREAKOUT"} or trend == "BEARISH":
            decision = "AVOID"
        elif overall >= 80 and not chase and (room_r is None or room_r >= 1.5):
            decision = "STRONG_CANDIDATE"
        elif overall >= 68 and not chase:
            decision = "CANDIDATE"
        elif overall >= 54:
            decision = "WATCH"
        else:
            decision = "AVOID"

        positives: List[str] = []
        risks: List[str] = []
        if trend in {"STRONG_BULLISH", "BULLISH"}:
            positives.append("Trend katmanı long yönünü destekliyor.")
        if structure == "HH_HL":
            positives.append("Onaylanmış swing yapısı HH + HL üretiyor.")
        if bullish_bos:
            positives.append("Son kapanış onaylanmış swing tepesinin üzerinde BOS oluşturdu.")
        if choch == "BULLISH_CHOCH":
            positives.append("Önceki zayıf yapı sonrası bullish CHoCH tespit edildi.")
        if breakout_state == "BREAKOUT_RETEST_HOLD":
            positives.append("Kırılım sonrası retest seviyesi korunuyor; giriş kalitesi yükseldi.")
        elif breakout_state == "BREAKOUT_CONFIRMED":
            positives.append("Önceki 20 bar tepe seviyesi hacim teyidiyle kırıldı.")
        if breakout_volume_confirmed:
            positives.append(f"Breakout hacmi teyitli; RVOL {rv:.2f}x.")
        if divergence == "BULLISH_RSI_DIVERGENCE":
            positives.append("Fiyat daha düşük dip yaparken RSI daha yüksek dip yaptı: bullish divergence.")
        if squeeze:
            positives.append("Bollinger genişliği ve ATR düşük yüzdelikte; volatilite sıkışması mevcut.")
        if location in {"NEAR_SUPPORT", "RETEST_ENTRY_ZONE"}:
            positives.append("Fiyat yapısal olarak daha kontrollü bir giriş bölgesinde.")

        if trend == "BEARISH":
            risks.append("EMA yapısı aşağı yönlü; long işlemi üst trendle çelişiyor.")
        if structure == "LH_LL":
            risks.append("Swing yapısı LH + LL; piyasa yapısı long için zayıf.")
        if bearish_bos:
            risks.append("Fiyat onaylanmış swing dibini kırdı; bearish BOS var.")
        if choch == "BEARISH_CHOCH":
            risks.append("Bullish yapının ardından bearish CHoCH oluştu.")
        if breakout_state in {"FALSE_BREAKOUT", "FAILED_BREAKOUT"}:
            risks.append("Kırılım seviyesi korunamadı; false/failed breakout riski aktif.")
        if divergence == "BEARISH_RSI_DIVERGENCE":
            risks.append("Fiyat yüksek tepe yaparken RSI zayıfladı: bearish divergence.")
        if chase:
            risks.append("Fiyat EMA21/direnç konumuna göre uzamış; chase riski yüksek.")
        if room_r is not None and room_r < 1.2:
            risks.append(f"En yakın dirence kadar alan yalnızca {room_r:.2f}R; risk/ödül zayıf.")
        if rv < 0.8:
            risks.append(f"RVOL {rv:.2f}x; hareket güçlü hacim teyidi almıyor.")
        if "EXHAUSTION_RISK" in candle["patterns"]:
            risks.append("Son mum ATR'ye göre aşırı geniş ve kapanış kalitesi zayıf; exhaustion riski var.")

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "decision": decision,
            "overall_score": overall,
            "components": components,
            "structure": {
                "state": structure,
                "bullish_bos": bullish_bos,
                "bearish_bos": bearish_bos,
                "choch": choch,
                "last_swing_high": round(last_swing_high, 4) if last_swing_high else None,
                "last_swing_low": round(last_swing_low, 4) if last_swing_low else None,
            },
            "breakout": {
                "state": breakout_state,
                "level": round(breakout_level, 4) if breakout_level else None,
                "bars_since_breakout": breakout_age,
                "volume_confirmed": breakout_volume_confirmed,
            },
            "candle": candle,
            "momentum": {
                "rsi14": round(last_rsi, 2),
                "macd_hist": round(last_macd_hist, 4),
                "divergence": divergence,
            },
            "volume": {
                "rvol": round(rv, 2),
                "trend": volume_trend,
                "breakout_volume_confirmed": breakout_volume_confirmed,
            },
            "volatility": {
                "bb_width_pct": round(last_bbw, 3),
                "bb_width_percentile": round(bbw_pctile, 1),
                "atr_pct": round(atr_pct, 3),
                "atr_percentile": round(atr_percentile, 1),
                "squeeze": squeeze,
                "expansion": expansion,
            },
            "location": {
                "state": location,
                "ema21_distance_atr": round(ema21_distance_atr, 2),
                "vwap_distance_atr": round(vwap_distance_atr, 2),
                "support": round(support, 4) if support else None,
                "resistance": round(resistance, 4) if resistance else None,
                "support_distance_atr": round(support_distance_atr, 2) if support_distance_atr is not None else None,
                "resistance_distance_atr": round(resistance_distance_atr, 2) if resistance_distance_atr is not None else None,
                "room_to_resistance_r": round(room_r, 2) if room_r is not None else None,
                "chase_risk": chase,
            },
            "trade_plan": {
                "entry": round(price, 4),
                "stop": round(stop, 4),
                "target1": round(target1, 4),
                "target2": round(target2, 4),
                "risk_per_share": round(risk, 4),
            },
            "trend": {
                "state": trend,
                "ema9": round(e9, 4),
                "ema21": round(e21, 4),
                "ema50": round(e50, 4),
                "ema200": round(e200, 4),
                "vwap": round(last_vwap, 4),
            },
            "positives": positives,
            "risks": risks,
            "method": {
                "name": "Chart Intelligence V2",
                "future_outcomes_used": False,
                "mutates_live_ranking": False,
                "features": [
                    "BOS/CHoCH", "false breakout", "breakout retest", "candle quality",
                    "RSI divergence", "volume confirmation", "volatility compression/expansion",
                    "support/resistance room", "EMA/VWAP extension", "chase detection", "structural risk plan",
                ],
            },
        }
