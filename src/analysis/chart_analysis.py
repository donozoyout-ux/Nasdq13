"""On-demand chart analysis for the Railway dashboard.

The service fetches point-in-time OHLCV data from Yahoo Finance, calculates
chart overlays and derives a deterministic, explainable technical reading.
It is intentionally separate from the ranking/backtest strategy so opening a
chart cannot mutate scanner state or change live ranking decisions.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class _CacheEntry:
    created_at: datetime
    payload: Dict[str, Any]


class ChartAnalysisService:
    """Fetch, calculate and explain one stock chart at a time."""

    TIMEFRAMES: Dict[str, Dict[str, str]] = {
        "15m": {"interval": "15m", "period": "60d"},
        "1h": {"interval": "1h", "period": "180d"},
        "1d": {"interval": "1d", "period": "1y"},
    }
    SYMBOL_RE = re.compile(r"^[A-Z0-9.\-\^=]{1,20}$")

    def __init__(self, timeout_seconds: int = 25, cache_ttl_seconds: int = 45):
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: Dict[Tuple[str, str], _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def analyze(self, symbol: str, timeframe: str = "15m") -> Dict[str, Any]:
        symbol = self._normalize_symbol(symbol)
        timeframe = timeframe.lower().strip()
        if timeframe not in self.TIMEFRAMES:
            raise ValueError("timeframe must be one of: 15m, 1h, 1d")

        key = (symbol, timeframe)
        now = datetime.now(timezone.utc)
        cached = self._cache.get(key)
        if cached and (now - cached.created_at).total_seconds() < self.cache_ttl_seconds:
            return cached.payload

        async with self._lock:
            cached = self._cache.get(key)
            if cached and (now - cached.created_at).total_seconds() < self.cache_ttl_seconds:
                return cached.payload

            df = await self._fetch(symbol, timeframe)
            payload = self._build_payload(symbol, timeframe, df)
            self._cache[key] = _CacheEntry(created_at=datetime.now(timezone.utc), payload=payload)
            return payload

    def _normalize_symbol(self, symbol: str) -> str:
        clean = (symbol or "").strip().upper()
        if not self.SYMBOL_RE.fullmatch(clean):
            raise ValueError("invalid symbol")
        return clean

    async def _fetch(self, symbol: str, timeframe: str) -> pd.DataFrame:
        spec = self.TIMEFRAMES[timeframe]
        loop = asyncio.get_running_loop()
        ticker = yf.Ticker(symbol)

        def _history() -> pd.DataFrame:
            return ticker.history(
                period=spec["period"],
                interval=spec["interval"],
                prepost=False,
                actions=False,
                auto_adjust=False,
                timeout=self.timeout_seconds,
            )

        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(None, _history),
                timeout=self.timeout_seconds + 8,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"{symbol} chart data timed out") from exc
        except Exception as exc:
            logger.error("Chart fetch failed for %s %s: %s", symbol, timeframe, exc)
            raise RuntimeError(f"{symbol} chart data could not be loaded") from exc

        if df is None or df.empty:
            raise RuntimeError(f"no chart data for {symbol}")

        df = df.copy()
        df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
        required = ["open", "high", "low", "close", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise RuntimeError(f"chart data missing columns: {', '.join(missing)}")

        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"]).sort_index()
        if len(df) < 60:
            raise RuntimeError(f"not enough chart history for {symbol} {timeframe}")

        return df.tail(260)

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).fillna(50.0)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()

    @staticmethod
    def _vwap(df: pd.DataFrame, timeframe: str) -> pd.Series:
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        volume = df["volume"].fillna(0.0)
        if timeframe == "1d":
            numerator = (typical * volume).rolling(20, min_periods=1).sum()
            denominator = volume.rolling(20, min_periods=1).sum().replace(0, np.nan)
            return (numerator / denominator).fillna(df["close"])

        session = pd.Series([str(ts.date()) for ts in df.index], index=df.index)
        pv = typical * volume
        numerator = pv.groupby(session).cumsum()
        denominator = volume.groupby(session).cumsum().replace(0, np.nan)
        return (numerator / denominator).fillna(df["close"])

    @staticmethod
    def _confirmed_pivots(df: pd.DataFrame, window: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
        highs: List[Tuple[int, float]] = []
        lows: List[Tuple[int, float]] = []
        for i in range(window, len(df) - window):
            hi = float(df["high"].iloc[i])
            lo = float(df["low"].iloc[i])
            hslice = df["high"].iloc[i - window:i + window + 1]
            lslice = df["low"].iloc[i - window:i + window + 1]
            if hi >= float(hslice.max()):
                highs.append((i, hi))
            if lo <= float(lslice.min()):
                lows.append((i, lo))
        return highs, lows

    @staticmethod
    def _cluster_levels(levels: List[Tuple[int, float]], tolerance: float, length: int) -> List[Dict[str, float]]:
        if not levels:
            return []
        clusters: List[Dict[str, Any]] = []
        for idx, price in sorted(levels, key=lambda item: item[0], reverse=True):
            recency = max(0.15, 1.0 - ((length - 1 - idx) / max(length, 1)))
            target = None
            for cluster in clusters:
                if abs(price - cluster["price"]) <= tolerance:
                    target = cluster
                    break
            if target is None:
                clusters.append({"price": price, "touches": 1, "weight": recency, "last_index": idx})
            else:
                total_weight = target["weight"] + recency
                target["price"] = ((target["price"] * target["weight"]) + (price * recency)) / total_weight
                target["weight"] = total_weight
                target["touches"] += 1
                target["last_index"] = max(target["last_index"], idx)
        return clusters

    @staticmethod
    def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            val = float(value)
            if math.isnan(val) or math.isinf(val):
                return default
            return val
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _series_payload(series: pd.Series, df: pd.DataFrame, digits: int = 4) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for ts, value in zip(df.index, series):
            val = ChartAnalysisService._safe_float(value)
            if val is None:
                continue
            out.append({"time": int(pd.Timestamp(ts).timestamp()), "value": round(val, digits)})
        return out

    def _build_payload(self, symbol: str, timeframe: str, df: pd.DataFrame) -> Dict[str, Any]:
        close = df["close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + 2.0 * bb_std
        bb_lower = bb_mid - 2.0 * bb_std
        vwap = self._vwap(df, timeframe)
        rsi = self._rsi(close, 14)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        macd_hist = macd - macd_signal
        atr = self._atr(df, 14)
        vol_sma20 = df["volume"].rolling(20).mean()
        rvol = df["volume"] / vol_sma20.replace(0, np.nan)
        donchian_upper = df["high"].shift(1).rolling(20).max()
        donchian_lower = df["low"].shift(1).rolling(20).min()

        price = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        last_atr = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else max(price * 0.02, 0.01)
        last_rsi = float(rsi.iloc[-1])
        last_rvol = self._safe_float(rvol.iloc[-1], 0.0) or 0.0
        last_vwap = float(vwap.iloc[-1])
        last_ema9 = float(ema9.iloc[-1])
        last_ema21 = float(ema21.iloc[-1])
        last_ema50 = float(ema50.iloc[-1])
        last_macd_hist = float(macd_hist.iloc[-1])
        last_donchian_upper = self._safe_float(donchian_upper.iloc[-1])
        last_donchian_lower = self._safe_float(donchian_lower.iloc[-1])

        pivot_highs, pivot_lows = self._confirmed_pivots(df, window=3)
        tolerance = max(last_atr * 0.35, price * 0.003)
        high_clusters = self._cluster_levels(pivot_highs[-30:], tolerance, len(df))
        low_clusters = self._cluster_levels(pivot_lows[-30:], tolerance, len(df))

        resistance_candidates = sorted([c for c in high_clusters if c["price"] > price * 1.0005], key=lambda c: c["price"])
        support_candidates = sorted([c for c in low_clusters if c["price"] < price * 0.9995], key=lambda c: c["price"], reverse=True)
        resistance = resistance_candidates[0]["price"] if resistance_candidates else last_donchian_upper
        support = support_candidates[0]["price"] if support_candidates else last_donchian_lower

        recent_highs = [p for _, p in pivot_highs[-3:]]
        recent_lows = [p for _, p in pivot_lows[-3:]]
        structure = "MIXED"
        if len(recent_highs) >= 2 and len(recent_lows) >= 2:
            if recent_highs[-1] > recent_highs[-2] and recent_lows[-1] > recent_lows[-2]:
                structure = "HH_HL"
            elif recent_highs[-1] < recent_highs[-2] and recent_lows[-1] < recent_lows[-2]:
                structure = "LH_LL"

        if price > last_ema9 > last_ema21 > last_ema50 and price >= last_vwap:
            trend = "STRONG_BULLISH"
        elif price > last_ema21 > last_ema50:
            trend = "BULLISH"
        elif price < last_ema9 < last_ema21 < last_ema50:
            trend = "BEARISH"
        else:
            trend = "TRANSITION"

        distance_to_res = ((resistance - price) / price * 100) if resistance and price else None
        distance_to_sup = ((price - support) / price * 100) if support and price else None
        breakout_confirmed = bool(last_donchian_upper and price > last_donchian_upper and last_rvol >= 1.2)
        near_resistance = bool(resistance and 0 <= resistance - price <= max(last_atr, price * 0.012))
        near_support = bool(support and 0 <= price - support <= max(last_atr, price * 0.012))
        bullish_last_candle = float(df["close"].iloc[-1]) > float(df["open"].iloc[-1])

        if breakout_confirmed:
            setup = "BREAKOUT_CONFIRMED"
        elif near_resistance and trend in {"STRONG_BULLISH", "BULLISH"}:
            setup = "BREAKOUT_READY"
        elif near_support and bullish_last_candle and trend != "BEARISH":
            setup = "SUPPORT_BOUNCE"
        elif trend in {"STRONG_BULLISH", "BULLISH"} and abs(price - last_ema21) <= last_atr:
            setup = "TREND_PULLBACK"
        elif trend == "TRANSITION":
            setup = "RANGE_OR_TRANSITION"
        else:
            setup = "NO_CLEAN_SETUP"

        score = 50.0
        score += {"STRONG_BULLISH": 18, "BULLISH": 10, "BEARISH": -18, "TRANSITION": 0}[trend]
        score += 10 if structure == "HH_HL" else (-10 if structure == "LH_LL" else 0)
        if 52 <= last_rsi <= 70:
            score += 10
        elif last_rsi < 42:
            score -= 8
        elif last_rsi > 76:
            score -= 5
        score += 6 if last_macd_hist > 0 else -4
        if last_rvol >= 1.5:
            score += 10
        elif last_rvol >= 1.2:
            score += 5
        elif last_rvol < 0.7:
            score -= 5
        if breakout_confirmed:
            score += 14
        elif near_resistance and trend in {"STRONG_BULLISH", "BULLISH"}:
            score += 5
        if near_support and trend != "BEARISH":
            score += 5
        score = round(float(np.clip(score, 0, 100)), 1)

        if score >= 80:
            decision = "STRONG_CANDIDATE"
        elif score >= 65:
            decision = "CANDIDATE"
        elif score >= 50:
            decision = "WATCH"
        else:
            decision = "AVOID"

        entry = price
        structural_stop = (support - 0.25 * last_atr) if support else (entry - 1.5 * last_atr)
        atr_stop = entry - 1.5 * last_atr
        stop = max(0.01, min(entry - 0.01, max(structural_stop, atr_stop)))
        risk = max(entry - stop, last_atr * 0.5, entry * 0.003)
        target1 = entry + 2.0 * risk
        target2 = entry + 3.0 * risk
        rr1 = (target1 - entry) / risk if risk > 0 else 0.0
        rr2 = (target2 - entry) / risk if risk > 0 else 0.0

        reasons: List[str] = []
        if trend == "STRONG_BULLISH":
            reasons.append("Fiyat EMA9 > EMA21 > EMA50 sıralamasının üzerinde; kısa ve orta vadeli trend uyumlu.")
        elif trend == "BULLISH":
            reasons.append("Fiyat EMA21 ve EMA50 üzerinde; ana eğilim yukarı fakat kısa vadeli hizalanma tam değil.")
        elif trend == "BEARISH":
            reasons.append("EMA dizilimi aşağı yönlü; long tarafında risk yükselmiş durumda.")
        else:
            reasons.append("EMA yapısı karışık; grafik geçiş/range bölgesinde.")

        if structure == "HH_HL":
            reasons.append("Onaylanmış swing yapısı Higher High + Higher Low üretiyor.")
        elif structure == "LH_LL":
            reasons.append("Onaylanmış swing yapısı Lower High + Lower Low üretiyor.")
        else:
            reasons.append("Swing tepeleri/dipleri henüz net bir HH/HL veya LH/LL dizilimi oluşturmuyor.")

        reasons.append(f"RSI(14) {last_rsi:.1f}; momentum {'pozitif bölgede' if last_rsi >= 50 else 'zayıf bölgede'}.")
        reasons.append(f"RVOL {last_rvol:.2f}x; hacim {'teyit veriyor' if last_rvol >= 1.2 else 'henüz güçlü teyit vermiyor'}.")
        if resistance:
            reasons.append(f"En yakın direnç {resistance:.2f}; fiyata mesafe %{max(distance_to_res or 0, 0):.2f}.")
        if support:
            reasons.append(f"En yakın destek {support:.2f}; fiyata mesafe %{max(distance_to_sup or 0, 0):.2f}.")
        if breakout_confirmed:
            reasons.append("Fiyat önceki 20 bar Donchian tepesini hacim teyidiyle aşmış: breakout teyitli.")
        elif near_resistance:
            reasons.append("Fiyat direnç bölgesine yakın; kırılım gelmeden kovalamak yerine teyit beklemek daha sağlıklı.")

        candles: List[Dict[str, Any]] = []
        volume_payload: List[Dict[str, Any]] = []
        for ts, row in df.iterrows():
            unix = int(pd.Timestamp(ts).timestamp())
            candle = {
                "time": unix,
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
            }
            candles.append(candle)
            vol = self._safe_float(row.get("volume"), 0.0) or 0.0
            volume_payload.append({"time": unix, "value": round(vol, 2), "direction": "up" if candle["close"] >= candle["open"] else "down"})

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bars": len(candles),
            "candles": candles,
            "volume": volume_payload,
            "indicators": {
                "ema9": self._series_payload(ema9, df),
                "ema21": self._series_payload(ema21, df),
                "ema50": self._series_payload(ema50, df),
                "vwap": self._series_payload(vwap, df),
                "bbUpper": self._series_payload(bb_upper, df),
                "bbMiddle": self._series_payload(bb_mid, df),
                "bbLower": self._series_payload(bb_lower, df),
            },
            "snapshot": {
                "price": round(price, 4),
                "change_pct": round(((price / prev_close) - 1) * 100, 3) if prev_close else 0.0,
                "trend": trend,
                "market_structure": structure,
                "setup": setup,
                "decision": decision,
                "score": score,
                "rsi14": round(last_rsi, 2),
                "rvol": round(last_rvol, 2),
                "macd_hist": round(last_macd_hist, 4),
                "atr14": round(last_atr, 4),
                "ema9": round(last_ema9, 4),
                "ema21": round(last_ema21, 4),
                "ema50": round(last_ema50, 4),
                "vwap": round(last_vwap, 4),
                "support": round(float(support), 4) if support else None,
                "resistance": round(float(resistance), 4) if resistance else None,
                "distance_to_support_pct": round(float(distance_to_sup), 3) if distance_to_sup is not None else None,
                "distance_to_resistance_pct": round(float(distance_to_res), 3) if distance_to_res is not None else None,
            },
            "trade_plan": {
                "entry": round(entry, 4),
                "stop": round(stop, 4),
                "target1": round(target1, 4),
                "target2": round(target2, 4),
                "rr1": round(rr1, 2),
                "rr2": round(rr2, 2),
                "risk_per_share": round(risk, 4),
            },
            "levels": {
                "support": round(float(support), 4) if support else None,
                "resistance": round(float(resistance), 4) if resistance else None,
                "donchian_upper_20": round(float(last_donchian_upper), 4) if last_donchian_upper else None,
                "donchian_lower_20": round(float(last_donchian_lower), 4) if last_donchian_lower else None,
            },
            "explanation": reasons,
            "method": {
                "name": "Deterministic Chart Reader v1",
                "principles": [
                    "trend before momentum",
                    "confirmed swing structure",
                    "support/resistance location",
                    "volume confirmation",
                    "breakout only against prior completed bars",
                    "ATR-based invalidation and targets",
                ],
            },
        }
