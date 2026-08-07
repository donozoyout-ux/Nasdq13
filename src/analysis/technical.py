"""
Technical Analysis Engine
- All indicators: EMA, RSI, MACD, Bollinger, ATR, VWAP, Donchian, StochRSI
- Multi-timeframe analysis
- Breakout detection
"""
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class IndicatorSnapshot:
    """Snapshot of all indicators at a point in time"""
    symbol: str
    timeframe: str

    # Price levels
    price: float
    open: float
    high: float
    low: float
    volume: float
    prev_close: float
    change_pct: float

    # Trend
    ema_9: float
    ema_21: float
    ema_50: float
    vwap: float

    # Momentum
    rsi_14: float
    macd: float
    macd_signal: float
    macd_hist: float
    stoch_rsi: float

    # Volatility
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_width_pct: float
    atr_14: float
    keltner_upper: float
    keltner_lower: float

    # Volume
    volume_sma_20: float
    volume_ratio: float
    obv: float
    obv_slope: float

    # Breakout
    donchian_upper_20: float
    donchian_lower_20: float
    gap_pct: float

    # Derived scores (0-100 scale)
    trend_score: float = 0.0
    momentum_score: float = 0.0
    volatility_score: float = 0.0
    volume_score: float = 0.0
    breakout_score: float = 0.0
    composite_score: float = 0.0

    # Detect flags
    is_breakout_up: bool = False
    is_breakout_down: bool = False
    is_volume_spike: bool = False
    is_vwap_reclaim: bool = False
    is_golden_cross: bool = False
    is_death_cross: bool = False
    is_overbought: bool = False
    is_oversold: bool = False


class TechnicalAnalyzer:
    """Computes all technical indicators and breakout detection"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.tech_config = config.get("technical", {})

        # Trend
        self.ema_short = self.tech_config.get("ema_short", 9)
        self.ema_medium = self.tech_config.get("ema_medium", 21)
        self.ema_long = self.tech_config.get("ema_long", 50)
        self.vwap_enabled = self.tech_config.get("vwap_enabled", True)

        # Momentum
        self.rsi_period = self.tech_config.get("rsi_period", 14)
        self.rsi_overbought = self.tech_config.get("rsi_overbought", 70)
        self.rsi_oversold = self.tech_config.get("rsi_oversold", 30)
        self.macd_fast = self.tech_config.get("macd_fast", 12)
        self.macd_slow = self.tech_config.get("macd_slow", 26)
        self.macd_signal = self.tech_config.get("macd_signal", 9)
        self.stoch_rsi_period = self.tech_config.get("stoch_rsi_period", 14)

        # Volatility
        self.bb_period = self.tech_config.get("bb_period", 20)
        self.bb_std = self.tech_config.get("bb_std", 2.0)
        self.keltner_period = self.tech_config.get("keltner_period", 20)
        self.keltner_multiplier = self.tech_config.get("keltner_multiplier", 2.0)
        self.atr_period = self.tech_config.get("atr_period", 14)

        # Volume
        self.volume_sma_period = self.tech_config.get("volume_sma_period", 20)
        self.volume_spike_multiplier = self.tech_config.get("volume_spike_multiplier", 2.0)
        self.obv_enabled = self.tech_config.get("obv_enabled", True)

        # Breakout
        self.donchian_period = self.tech_config.get("donchian_period", 20)
        self.gap_threshold_pct = self.tech_config.get("gap_threshold_pct", 0.5)
        self.vwap_reclaim_threshold = self.tech_config.get("vwap_reclaim_threshold", 0.001)

        # Signal weights
        self.weights = config.get("signal_weights", {})
        self.w_breakout = self.weights.get("breakout_strength", 30)
        self.w_volume = self.weights.get("volume_confirmation", 25)
        self.w_trend = self.weights.get("trend_alignment", 20)
        self.w_momentum = self.weights.get("momentum", 15)
        self.w_support = self.weights.get("support_resistance", 10)

    # ------------------------------------------------------------------
    # Indicator calculations (all numpy/pandas vectorized - FAST)
    # ------------------------------------------------------------------

    def _calc_ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    def _calc_sma(self, series: pd.Series, period: int) -> pd.Series:
        return series.rolling(window=period).mean()

    def _calc_rsi(self, series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)

    def _calc_macd(self, series: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        ema_fast = self._calc_ema(series, self.macd_fast)
        ema_slow = self._calc_ema(series, self.macd_slow)
        macd_line = ema_fast - ema_slow
        signal_line = self._calc_ema(macd_line, self.macd_signal)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def _calc_bollinger(self, series: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        middle = self._calc_sma(series, self.bb_period)
        std = series.rolling(window=self.bb_period).std()
        upper = middle + (self.bb_std * std)
        lower = middle - (self.bb_std * std)
        return upper, middle, lower

    def _calc_atr(self, df: pd.DataFrame) -> pd.Series:
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return true_range.ewm(alpha=1 / self.atr_period, adjust=False).mean()

    def _calc_vwap(self, df: pd.DataFrame) -> pd.Series:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
        return vwap

    def _calc_stoch_rsi(self, series: pd.Series) -> pd.Series:
        rsi = self._calc_rsi(series, self.stoch_rsi_period)
        rsi_min = rsi.rolling(window=self.stoch_rsi_period).min()
        rsi_max = rsi.rolling(window=self.stoch_rsi_period).max()
        stoch = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
        return stoch.fillna(0.5) * 100

    def _calc_obv(self, df: pd.DataFrame) -> pd.Series:
        direction = np.sign(df["close"].diff().fillna(0))
        obv = (direction * df["volume"]).cumsum()
        return obv

    def _calc_donchian(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        upper = df["high"].rolling(window=self.donchian_period).max()
        lower = df["low"].rolling(window=self.donchian_period).min()
        return upper, lower

    # ------------------------------------------------------------------
    # Scoring functions (0-100 scale per component)
    # ------------------------------------------------------------------

    def _score_trend(self, df: pd.DataFrame, s: IndicatorSnapshot) -> float:
        """Score trend alignment (0 bearish - 100 bullish, 50 neutral)"""
        score = 50.0

        if s.price > s.ema_9 > s.ema_21 > s.ema_50:
            score += 30
        elif s.price > s.ema_9 and s.ema_9 > s.ema_21:
            score += 20
        elif s.price > s.ema_9:
            score += 10
        elif s.price < s.ema_9 < s.ema_21 < s.ema_50:
            score -= 30
        elif s.price < s.ema_9 and s.ema_9 < s.ema_21:
            score -= 20
        elif s.price < s.ema_9:
            score -= 10

        if s.vwap > 0:
            if s.price > s.vwap:
                score += 10
            else:
                score -= 10

        if s.is_golden_cross:
            score += 10
        if s.is_death_cross:
            score -= 10

        return np.clip(score, 0, 100)

    def _score_momentum(self, s: IndicatorSnapshot) -> float:
        """Score momentum (0 bearish - 100 bullish)"""
        score = 50.0

        if 55 <= s.rsi_14 <= 70:
            score += 15
        elif 30 <= s.rsi_14 < 45:
            score -= 15
        elif s.rsi_14 > 70:
            score -= 5
        elif s.rsi_14 < 30:
            score += 5
        elif 45 <= s.rsi_14 < 55:
            score += 5

        if s.macd_hist > 0 and s.macd > s.macd_signal:
            score += 15
        elif s.macd_hist < 0 and s.macd < s.macd_signal:
            score -= 15
        elif s.macd_hist > 0:
            score += 10
        elif s.macd_hist < 0:
            score -= 10

        if s.stoch_rsi > 80:
            score += 10
        elif s.stoch_rsi > 50:
            score += 5
        elif s.stoch_rsi < 20:
            score -= 10
        elif s.stoch_rsi < 50:
            score -= 5

        return np.clip(score, 0, 100)

    def _score_volatility(self, s: IndicatorSnapshot) -> float:
        """Score volatility positioning (using Bollinger %B equivalent)"""
        if s.bb_upper == s.bb_lower:
            return 50.0

        percent_b = (s.price - s.bb_lower) / (s.bb_upper - s.bb_lower)
        score = percent_b * 100

        if s.price > s.bb_upper:
            score = 90
        elif s.price < s.bb_lower:
            score = 10

        return np.clip(score, 0, 100)

    def _score_volume(self, s: IndicatorSnapshot) -> float:
        """Score volume confirmation"""
        score = 50.0

        if s.volume_ratio >= self.volume_spike_multiplier:
            score += 25
        elif s.volume_ratio >= 1.5:
            score += 15
        elif s.volume_ratio >= 1.2:
            score += 8
        elif s.volume_ratio < 0.7:
            score -= 15
        elif s.volume_ratio < 0.5:
            score -= 25

        if s.obv_slope > 0:
            score += 15
        elif s.obv_slope < 0:
            score -= 15

        return np.clip(score, 0, 100)

    def _score_breakout(self, s: IndicatorSnapshot) -> float:
        """Score breakout strength"""
        score = 50.0

        if s.price > s.donchian_upper_20:
            score += 30
        elif s.price < s.donchian_lower_20:
            score -= 30

        if s.price > s.bb_upper:
            score += 20
        elif s.price < s.bb_lower:
            score -= 20

        if s.gap_pct > self.gap_threshold_pct:
            score += 20
        elif s.gap_pct < -self.gap_threshold_pct:
            score -= 20

        if s.is_vwap_reclaim:
            score += 15

        if s.change_pct > 1.0:
            score += 15
        elif s.change_pct > 0.5:
            score += 10
        elif s.change_pct < -1.0:
            score -= 15
        elif s.change_pct < -0.5:
            score -= 10

        return np.clip(score, 0, 100)

    def _calculate_composite(self, parts: Dict[str, float], direction: int) -> float:
        """Combine weighted scores into composite (-100 to +100)"""
        composite = 0.0
        for name, weight in [
            ("breakout", self.w_breakout),
            ("volume", self.w_volume),
            ("trend", self.w_trend),
            ("momentum", self.w_momentum),
            ("volatility", self.w_support),
        ]:
            score = parts.get(name, 50.0)
            deviation = (score - 50) * 2
            composite += deviation * (weight / 100.0)

        return composite * direction

    # ------------------------------------------------------------------
    # Main analysis pipeline
    # ------------------------------------------------------------------

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[IndicatorSnapshot]:
        """Run full technical analysis on a dataframe"""
        if df is None or len(df) < 60:
            logger.warning(f"Insufficient data for analysis: {symbol} {timeframe}")
            return None

        try:
            df = df.copy()
            df.columns = [c.lower().replace(" ", "_") for c in df.columns]

            # --- Calculate all indicators ---
            df["ema_9"] = self._calc_ema(df["close"], self.ema_short)
            df["ema_21"] = self._calc_ema(df["close"], self.ema_medium)
            df["ema_50"] = self._calc_ema(df["close"], self.ema_long)
            if self.vwap_enabled:
                df["vwap"] = self._calc_vwap(df)

            df["rsi_14"] = self._calc_rsi(df["close"], self.rsi_period)
            df["macd"], df["macd_signal"], df["macd_hist"] = self._calc_macd(df["close"])
            df["stoch_rsi"] = self._calc_stoch_rsi(df["close"])

            df["bb_upper"], df["bb_middle"], df["bb_lower"] = self._calc_bollinger(df["close"])
            df["atr_14"] = self._calc_atr(df)
            atr_mean = df["atr_14"].rolling(window=self.keltner_period).mean()
            df["keltner_upper"] = df["ema_21"] + (self.keltner_multiplier * atr_mean)
            df["keltner_lower"] = df["ema_21"] - (self.keltner_multiplier * atr_mean)

            df["volume_sma_20"] = df["volume"].rolling(window=self.volume_sma_period).mean()
            if self.obv_enabled:
                df["obv"] = self._calc_obv(df)
                df["obv_sma_10"] = df["obv"].rolling(window=10).mean()

            df["donchian_upper_20"], df["donchian_lower_20"] = self._calc_donchian(df)

            # --- Get latest values ---
            last = df.iloc[-1]
            prev = df.iloc[-2]

            price = float(last["close"])
            prev_close = float(prev["close"])
            change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0

            gap_pct = ((float(last["open"]) - prev_close) / prev_close) * 100 if prev_close else 0.0

            volume_sma = float(last["volume_sma_20"]) if pd.notna(last["volume_sma_20"]) else 0
            volume_ratio = float(last["volume"]) / volume_sma if volume_sma > 0 else 0

            obv_slope = 0.0
            if self.obv_enabled and "obv" in df.columns and "obv_sma_10" in df.columns:
                obv_values = df["obv"].dropna()
                if len(obv_values) >= 10:
                    obv_slope = float(np.polyfit(range(10), obv_values.tail(10).values, 1)[0])

            ema9_prev, ema21_prev = float(prev.get("ema_9", 0)), float(prev.get("ema_21", 0))
            ema9_curr, ema21_curr = float(last["ema_9"]), float(last["ema_21"])
            golden_cross = ema9_prev <= ema21_prev and ema9_curr > ema21_curr
            death_cross = ema9_prev >= ema21_prev and ema9_curr < ema21_curr

            vwap_prev = float(prev.get("vwap", 0)) if pd.notna(prev.get("vwap", 0)) else 0
            vwap_curr = float(last["vwap"]) if pd.notna(last.get("vwap", 0)) else 0
            vwap_reclaim = (vwap_curr > 0 and vwap_prev > 0 and
                            prev_close <= vwap_prev and price > vwap_curr)

            donchian_upper = float(last["donchian_upper_20"]) if pd.notna(last["donchian_upper_20"]) else price
            donchian_lower = float(last["donchian_lower_20"]) if pd.notna(last["donchian_lower_20"]) else price
            bb_upper = float(last["bb_upper"]) if pd.notna(last["bb_upper"]) else price
            bb_lower = float(last["bb_lower"]) if pd.notna(last["bb_lower"]) else price

            is_breakout_up = price > donchian_upper or price > bb_upper
            is_breakout_down = price < donchian_lower or price < bb_lower
            is_volume_spike = volume_ratio >= self.volume_spike_multiplier

            rsi = float(last["rsi_14"]) if pd.notna(last["rsi_14"]) else 50

            # --- Build snapshot ---
            snap = IndicatorSnapshot(
                symbol=symbol,
                timeframe=timeframe,
                price=price,
                open=float(last["open"]),
                high=float(last["high"]),
                low=float(last["low"]),
                volume=float(last["volume"]),
                prev_close=prev_close,
                change_pct=change_pct,
                ema_9=float(last["ema_9"]) if pd.notna(last["ema_9"]) else price,
                ema_21=float(last["ema_21"]) if pd.notna(last["ema_21"]) else price,
                ema_50=float(last["ema_50"]) if pd.notna(last["ema_50"]) else price,
                vwap=float(last["vwap"]) if pd.notna(last.get("vwap", 0)) else 0,
                rsi_14=rsi,
                macd=float(last["macd"]) if pd.notna(last["macd"]) else 0,
                macd_signal=float(last["macd_signal"]) if pd.notna(last["macd_signal"]) else 0,
                macd_hist=float(last["macd_hist"]) if pd.notna(last["macd_hist"]) else 0,
                stoch_rsi=float(last["stoch_rsi"]) if pd.notna(last["stoch_rsi"]) else 50,
                bb_upper=bb_upper,
                bb_middle=float(last["bb_middle"]) if pd.notna(last["bb_middle"]) else price,
                bb_lower=bb_lower,
                bb_width_pct=((bb_upper - bb_lower) / bb_middle * 100) if bb_middle else 0,
                atr_14=float(last["atr_14"]) if pd.notna(last["atr_14"]) else 0,
                keltner_upper=float(last["keltner_upper"]) if pd.notna(last["keltner_upper"]) else price,
                keltner_lower=float(last["keltner_lower"]) if pd.notna(last["keltner_lower"]) else price,
                volume_sma_20=volume_sma,
                volume_ratio=volume_ratio,
                obv=float(last.get("obv", 0)) if self.obv_enabled else 0,
                obv_slope=obv_slope,
                donchian_upper_20=donchian_upper,
                donchian_lower_20=donchian_lower,
                gap_pct=gap_pct,
                is_breakout_up=is_breakout_up,
                is_breakout_down=is_breakout_down,
                is_volume_spike=is_volume_spike,
                is_vwap_reclaim=vwap_reclaim,
                is_golden_cross=golden_cross,
                is_death_cross=death_cross,
                is_overbought=rsi > self.rsi_overbought,
                is_oversold=rsi < self.rsi_oversold,
            )

            # --- Calculate scores ---
            snap.trend_score = self._score_trend(df, snap)
            snap.momentum_score = self._score_momentum(snap)
            snap.volatility_score = self._score_volatility(snap)
            snap.volume_score = self._score_volume(snap)
            snap.breakout_score = self._score_breakout(snap)

            direction = 1
            if (snap.is_breakout_down or snap.is_death_cross or
                    snap.rsi_14 < self.rsi_oversold or snap.change_pct < -1):
                direction = -1

            snap.composite_score = self._calculate_composite(
                {
                    "breakout": snap.breakout_score,
                    "volume": snap.volume_score,
                    "trend": snap.trend_score,
                    "momentum": snap.momentum_score,
                    "volatility": snap.volatility_score,
                },
                direction
            )

            return snap

        except Exception as e:
            logger.error(f"Analysis error for {symbol} {timeframe}: {e}")
            logger.exception(e)
            return None

    def analyze_all(self, data: Dict[str, Dict[str, pd.DataFrame]]) -> Dict[str, Dict[str, IndicatorSnapshot]]:
        """Analyze all symbols and timeframes"""
        results = {}
        for symbol, tfs in data.items():
            results[symbol] = {}
            for tf, df in tfs.items():
                snap = self.analyze(df, symbol, tf)
                if snap:
                    results[symbol][tf] = snap
        return results

    def get_market_regime(self, daily_df: pd.DataFrame) -> str:
        """Determine market regime from daily data"""
        if daily_df is None or len(daily_df) < 50:
            return "unknown"
        try:
            df = daily_df.copy()
            df["ema_20"] = self._calc_ema(df["close"], 20)
            df["ema_50"] = self._calc_ema(df["close"], 50)

            price = float(df["close"].iloc[-1])
            ema20 = float(df["ema_20"].iloc[-1])
            ema50 = float(df["ema_50"].iloc[-1])

            rsi = self._calc_rsi(df["close"], 14).iloc[-1]
            rsi = float(rsi) if pd.notna(rsi) else 50

            if price > ema20 > ema50 and rsi > 55:
                return "bullish"
            elif price < ema20 < ema50 and rsi < 45:
                return "bearish"
            elif rsi > 70:
                return "overbought"
            elif rsi < 30:
                return "oversold"
            else:
                return "neutral"
        except Exception as e:
            logger.error(f"Market regime error: {e}")
            return "unknown"
