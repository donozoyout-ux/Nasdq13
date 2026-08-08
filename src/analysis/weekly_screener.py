"""
Weekly/Monthly Screener
- Scans a stock universe on 1wk / 1mo / 1d timeframes
- Ranks candidates by "sudden breakout" setup probability (free, rule-based)
- Picks candidates automatically from the universe (no manual list needed)
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

import numpy as np
import pandas as pd

from src.utils.logger import get_logger
from src.data.price_fetcher import PriceFetcher, PriceData
from src.analysis.technical import TechnicalAnalyzer, IndicatorSnapshot

logger = get_logger(__name__)


@dataclass
class BreakoutCandidate:
    """A stock ranked by its breakout (sudden move) setup"""
    symbol: str
    name: str
    price: float
    change_pct: float
    setup_score: float          # 0-100 weekly breakout setup strength
    setup_type: str             # squeeze | basing | trend | momentum | watch
    reasons: List[str] = field(default_factory=list)

    # Weekly detail
    weekly_return_4w: float = 0.0
    weekly_return_8w: float = 0.0
    rsi_14: float = 50.0
    macd_hist: float = 0.0
    bb_width_pct: float = 0.0
    bb_width_percentile: float = 0.0   # 0-100 ; dÃ¼ÅŸÃ¼k = sÄ±kÄ±ÅŸma (squeeze)
    vol_ratio: float = 1.0
    atr_pct: float = 0.0               # haftalÄ±k ATR'nin fiyata oranÄ± (%)
    dist_52w_high_pct: float = 0.0     # 52 haftalÄ±k zirveye uzaklÄ±k (%)
    rs_4w: float = 0.0                 # S&P 500'e gÃ¶re 4 haftalÄ±k rÃ¶latif gÃ¼Ã§ (%)

    # Daily trigger (gÃ¼nlÃ¼k brifing iÃ§in)
    daily_score: float = 0.0
    daily_triggers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": round(self.price, 2),
            "change_pct": round(self.change_pct, 2),
            "setup_score": round(self.setup_score, 1),
            "setup_type": self.setup_type,
            "reasons": self.reasons,
            "weekly_return_4w": round(self.weekly_return_4w, 2),
            "weekly_return_8w": round(self.weekly_return_8w, 2),
            "rsi_14": round(self.rsi_14, 1),
            "macd_hist": round(self.macd_hist, 3),
            "bb_width_pct": round(self.bb_width_pct, 2),
            "bb_width_percentile": round(self.bb_width_percentile, 1),
            "vol_ratio": round(self.vol_ratio, 2),
            "atr_pct": round(self.atr_pct, 2),
            "dist_52w_high_pct": round(self.dist_52w_high_pct, 2),
            "rs_4w": round(self.rs_4w, 2),
            "daily_score": round(self.daily_score, 1),
            "daily_triggers": self.daily_triggers,
        }


SETUP_TURKISH = {
    "squeeze": "Volatilite SÄ±kÄ±ÅŸmasÄ± (Squeeze)",
    "basing": "Zirveye DayalÄ± Konsolidasyon",
    "trend": "GÃ¼Ã§lÃ¼ Trend DevamÄ±",
    "momentum": "Ä°vme / Momentum",
    "watch": "Ä°zleme Listesi",
}


class WeeklyScreener:
    """Rule-based breakout setup screener over a stock universe"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.sc_config = config.get("screener", {})

        raw_universe = self.sc_config.get("universe", [])
        self.universe = self._flatten_universe(raw_universe)
        self.index_symbol = self.sc_config.get("index_symbol", "^GSPC")

        self.top_n = int(self.sc_config.get("top_n", 12))
        self.min_score = float(self.sc_config.get("min_setup_score", 50))
        self.min_price = float(self.sc_config.get("min_price", 3.0))
        self.lookback_weeks = int(self.sc_config.get("lookback_weeks", 60))

        self._symbol_names = {
            **{
                "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
                "GOOGL": "Alphabet", "AMZN": "Amazon", "META": "Meta",
                "TSLA": "Tesla", "AMD": "AMD", "INTC": "Intel", "MU": "Micron",
                "AVGO": "Broadcom", "QCOM": "Qualcomm", "ADBE": "Adobe",
                "CRM": "Salesforce", "ORCL": "Oracle", "PLTR": "Palantir",
                "CRWD": "CrowdStrike", "SHOP": "Shopify", "UBER": "Uber",
                "PYPL": "PayPal", "JPM": "JPMorgan", "GS": "Goldman Sachs",
                "BAC": "Bank of America", "V": "Visa", "MA": "Mastercard",
                "COIN": "Coinbase", "HOOD": "Robinhood", "XOM": "Exxon",
                "CVX": "Chevron", "OXY": "Occidental", "SLB": "Schlumberger",
                "COP": "ConocoPhillips", "PFE": "Pfizer", "MRNA": "Moderna",
                "LLY": "Eli Lilly", "AMGN": "Amgen", "UNH": "UnitedHealth",
                "JNJ": "Johnson & Johnson", "NKE": "Nike", "MCD": "McDonald's",
                "HD": "Home Depot", "COST": "Costco", "WMT": "Walmart",
                "BA": "Boeing", "CAT": "Caterpillar", "GE": "GE Aerospace",
                "LMT": "Lockheed", "RIVN": "Rivian", "NIO": "NIO", "ABNB": "Airbnb",
                "^GSPC": "S&P 500",
            },
            **config.get("symbol_names", {}),
        }

        fetcher_config = {
            "symbols": [self.index_symbol] + self.universe,
            "timeframes": ["1wk", "1mo", "1d"],
            "scanner": {
                "max_concurrent_requests": self.sc_config.get("max_concurrent_requests", 6),
                "request_timeout": self.sc_config.get("request_timeout", 60),
            },
        }
        self.price_fetcher = PriceFetcher(fetcher_config)
        self.analyzer = TechnicalAnalyzer(config)

    @staticmethod
    def _flatten_universe(raw: List[str]) -> List[str]:
        """Flatten YAML list that may contain comma-separated strings"""
        symbols: List[str] = []
        for item in raw:
            for part in str(item).split(","):
                part = part.strip()
                if part and part not in symbols:
                    symbols.append(part)
        return symbols

    def display_name(self, symbol: str) -> str:
        return self._symbol_names.get(symbol, symbol)

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    async def _fetch_all(self) -> Dict[str, Dict[str, PriceData]]:
        """Fetch weekly/monthly/daily data for index + universe"""
        return await self.price_fetcher.fetch_all()

    # ------------------------------------------------------------------
    # Weekly setup scoring
    # ------------------------------------------------------------------

    def _weekly_metrics(self, df: pd.DataFrame, snap: IndicatorSnapshot) -> Dict[str, Any]:
        """Compute extra weekly metrics not in the standard snapshot"""
        close = df["close"]
        high = df["high"]
        low = df["low"]

        ret_4w = float(close.iloc[-1] / close.iloc[-5] - 1) * 100 if len(close) >= 5 else 0.0
        ret_8w = float(close.iloc[-1] / close.iloc[-9] - 1) * 100 if len(close) >= 9 else ret_4w

        # 52-week high (approx 52 bars)
        n52 = min(52, len(close))
        high_52 = float(high.tail(n52).max())
        dist_52w = (snap.price / high_52 - 1) * 100 if high_52 > 0 else 0.0

        # Bollinger width percentile (weekly) - lower = tighter squeeze
        bb_period = self.analyzer.bb_period
        mid = close.rolling(window=bb_period).mean()
        std = close.rolling(window=bb_period).std()
        width = (2 * self.analyzer.bb_std * std) / mid * 100
        width = width.replace([np.inf, -np.inf], np.nan).dropna()
        bbw = float(width.iloc[-1]) if len(width) else 0.0
        if len(width) > 20:
            bbw_pctile = float((width.rank(pct=True)).iloc[-1] * 100)
        else:
            bbw_pctile = 50.0

        # Weekly ATR as % of price
        atr_pct = (snap.atr_14 / snap.price * 100) if snap.price > 0 and snap.atr_14 > 0 else 0.0

        return {
            "ret_4w": ret_4w,
            "ret_8w": ret_8w,
            "dist_52w_high_pct": dist_52w,
            "bb_width_pct": bbw,
            "bb_width_percentile": bbw_pctile,
            "atr_pct": atr_pct,
        }

    def _score_setup(self, snap: IndicatorSnapshot, m: Dict[str, Any], rs_4w: float) -> Tuple[float, str]:
        """Compute weekly breakout-setup score (0-100) and setup type"""
        score = 0.0
        bbw_pctile = m["bb_width_percentile"]
        dist = m["dist_52w_high_pct"]
        ema_ok = snap.price > snap.ema_21 > snap.ema_50
        ema_partial = snap.price > snap.ema_21

        # 1) Squeeze / volatility compression (0-30)
        if bbw_pctile < 25:
            score += (25 - bbw_pctile) * (30 / 25)
        elif bbw_pctile < 50:
            score += 10 * (1 - bbw_pctile / 50)

        # 2) Proximity to 52-week high (0-25)
        if dist <= 0:
            score += 25
        elif dist <= 3:
            score += 20
        elif dist <= 7:
            score += 12
        elif dist <= 12:
            score += 5

        # 3) Relative strength vs index (0-20)
        score += float(np.clip(5 + rs_4w * 2, 0, 20))

        # 4) Trend alignment (0-15)
        if ema_ok:
            score += 12
        elif ema_partial:
            score += 7
        if snap.is_golden_cross:
            score += 5
        elif snap.is_death_cross:
            score -= 8

        # 5) Momentum (0-10)
        if 50 <= snap.rsi_14 <= 72 and snap.macd_hist > 0:
            score += 8
        elif 45 <= snap.rsi_14 <= 78:
            score += 4
        if snap.rsi_14 > 82:
            score -= 5

        # 6) Volume behavior (0-10)
        if snap.volume_ratio >= 1.5:
            score += 8
        elif snap.volume_ratio >= 1.2:
            score += 4
        elif snap.volume_ratio < 0.8:
            score += 3  # volume contraction = accumulated fuel

        score = float(np.clip(score, 0, 100))

        # Setup type
        already_moved = abs(snap.change_pct) >= 8.0
        if bbw_pctile < 25 and not already_moved:
            stype = "squeeze"
        elif bbw_pctile < 25 and already_moved:
            stype = "trend"
        elif dist <= 0:
            stype = "basing"
        elif ema_ok and rs_4w > 0:
            stype = "trend"
        elif snap.macd_hist > 0 and 50 <= snap.rsi_14 <= 72:
            stype = "momentum"
        else:
            stype = "watch"

        return score, stype

    def _build_reasons(self, snap: IndicatorSnapshot, m: Dict[str, Any], stype: str, rs_4w: float) -> List[str]:
        reasons = []
        if stype == "squeeze":
            reasons.append(f"📐 Bollinger bant genişliği son {self.lookback_weeks} haftanın %{m['bb_width_percentile']:.0f}'inde (sıkışma)")
        if m["dist_52w_high_pct"] <= 0:
            reasons.append("🎯 52 haftalık zirvede / zirveye dayanmış")
        elif m["dist_52w_high_pct"] <= 3:
            reasons.append(f"🎯 52 haftalık zirvenin %{m['dist_52w_high_pct']:.1f} altında (çok yakın)")
        if snap.price > snap.ema_21 > snap.ema_50:
            reasons.append("📈 Fiyat EMA21 > EMA50 üzerinde (yükseliş trendi)")
        if snap.is_golden_cross:
            reasons.append("✨ Haftalık Golden Cross")
        if rs_4w > 0:
            reasons.append(f"⚡ S&P 500'e göre son 4 haftada +{rs_4w:.1f} puan güçlü")
        if 55 <= snap.rsi_14 <= 72:
            reasons.append(f"🔥 RSI {snap.rsi_14:.0f} (güçlü momentum, aşırı alım değil)")
        elif snap.rsi_14 > 72:
            reasons.append(f"⚠️ RSI {snap.rsi_14:.0f} (aşırı alım bölgesi)")
        if snap.volume_ratio >= 1.5:
            reasons.append(f"🔊 Haftalık hacim artışı ({snap.volume_ratio:.1f}x)")
        if snap.volume_ratio < 0.8:
            reasons.append("🔇 Haftalık hacim daralması (sıkışma birikimi)")
        return reasons

    # ------------------------------------------------------------------
    # Daily trigger scoring
    # ------------------------------------------------------------------

    def _score_daily(self, daily_df: pd.DataFrame, snap_d: Optional[IndicatorSnapshot]) -> Tuple[float, List[str]]:
        """Score today's breakout trigger (0-100) from daily data"""
        if snap_d is None:
            return 0.0, []

        score = 50.0
        triggers = []
        df = daily_df

        donchian_upper = float(df["high"].rolling(window=self.analyzer.donchian_period).max().iloc[-1])
        gap_pct = snap_d.gap_pct
        chg = snap_d.change_pct
        vr = snap_d.volume_ratio

        if snap_d.price > donchian_upper:
            score += 25
            triggers.append("ğŸ“ˆ GÃ¼nlÃ¼k Donchian Ã¼st bandÄ± kÄ±rÄ±ldÄ±")
        elif snap_d.price >= donchian_upper * 0.99:
            score += 15
            triggers.append("ğŸ¯ GÃ¼nlÃ¼k Donchian Ã¼st bandÄ±na dayandÄ±")

        if vr >= 2.0:
            score += 15
            triggers.append(f"ğŸ”Š Hacim patlamasÄ± ({vr:.1f}x)")
        elif vr >= 1.5:
            score += 10

        if 55 <= snap_d.rsi_14 <= 75:
            score += 10
        elif snap_d.rsi_14 > 80:
            score -= 5

        if snap_d.macd_hist > 0 and snap_d.macd > snap_d.macd_signal:
            score += 10
            triggers.append("ğŸ’¹ MACD pozitif kesiÅŸim")

        if chg > 1.0:
            score += 10
            triggers.append(f"ğŸš€ GÃ¼nlÃ¼k {chg:+.1f}% hareket")
        if gap_pct > 0.5:
            score += 10
            triggers.append("ğŸŒ… YÃ¼ksek aÃ§Ä±lÄ±ÅŸ (gap up)")

        return float(np.clip(score, 0, 100)), triggers

    # ------------------------------------------------------------------
    # Main screen
    # ------------------------------------------------------------------

    async def screen(self) -> Tuple[List[BreakoutCandidate], Dict[str, Any]]:
        """
        Screen universe and return ranked weekly breakout candidates
        plus index (S&P 500) context stats.
        """
        price_data = await self._fetch_all()

        # Index context
        index_stats: Dict[str, Any] = {"symbol": self.index_symbol, "name": "S&P 500"}
        idx_dfs = price_data.get(self.index_symbol, {})
        idx_wk = idx_dfs.get("1wk")
        if idx_wk is not None:
            c = idx_wk.data["close"]
            if len(c) >= 5:
                index_stats["return_4w"] = round(float(c.iloc[-1] / c.iloc[-5] - 1) * 100, 2)
            if len(c) >= 9:
                index_stats["return_8w"] = round(float(c.iloc[-1] / c.iloc[-9] - 1) * 100, 2)
            idx_snap = self.analyzer.analyze(idx_wk.data, self.index_symbol, "1wk")
            if idx_snap:
                index_stats["rsi"] = round(idx_snap.rsi_14, 1)
                index_stats["trend"] = ("bullish" if idx_snap.price > idx_snap.ema_21 > idx_snap.ema_50
                                        else "bearish" if idx_snap.price < idx_snap.ema_21 < idx_snap.ema_50
                                        else "neutral")

        candidates: List[BreakoutCandidate] = []
        skipped = 0

        for symbol in self.universe:
            try:
                tfs = price_data.get(symbol, {})
                wk = tfs.get("1wk")
                mo = tfs.get("1mo")
                day = tfs.get("1d")

                if wk is None or wk.data is None or len(wk.data) < 60:
                    skipped += 1
                    continue

                snap = self.analyzer.analyze(wk.data, symbol, "1wk")
                if snap is None or snap.price < self.min_price:
                    skipped += 1
                    continue

                m = self._weekly_metrics(wk.data, snap)

                # Relative strength vs index over 4 weeks
                idx_ret = index_stats.get("return_4w", 0.0)
                rs_4w = m["ret_4w"] - idx_ret

                score, stype = self._score_setup(snap, m, rs_4w)
                if score < self.min_score:
                    continue

                reasons = self._build_reasons(snap, m, stype, rs_4w)

                # Daily trigger (optional - monthly trend awareness)
                snap_d = self.analyzer.analyze(day.data, symbol, "1d") if day is not None else None
                daily_score, daily_triggers = self._score_daily(day.data, snap_d) if day is not None else (0.0, [])

                cand = BreakoutCandidate(
                    symbol=symbol,
                    name=self.display_name(symbol),
                    price=snap.price,
                    change_pct=snap.change_pct,
                    setup_score=score,
                    setup_type=stype,
                    reasons=reasons,
                    weekly_return_4w=m["ret_4w"],
                    weekly_return_8w=m["ret_8w"],
                    rsi_14=snap.rsi_14,
                    macd_hist=snap.macd_hist,
                    bb_width_pct=m["bb_width_pct"],
                    bb_width_percentile=m["bb_width_percentile"],
                    vol_ratio=snap.volume_ratio,
                    atr_pct=m["atr_pct"],
                    dist_52w_high_pct=m["dist_52w_high_pct"],
                    rs_4w=rs_4w,
                    daily_score=daily_score,
                    daily_triggers=daily_triggers,
                )
                candidates.append(cand)
            except Exception as e:
                logger.warning(f"Screener error for {symbol}: {e}")

        candidates.sort(key=lambda c: c.setup_score, reverse=True)
        index_stats["scanned"] = len(self.universe)
        index_stats["candidates"] = len(candidates)
        index_stats["skipped"] = skipped

        logger.info(f"Screener: {len(self.universe)} tarandÄ±, "
                    f"{len(candidates)} aday bulundu (index 4h: {index_stats.get('return_4w')}%)")
        return candidates, index_stats

    async def screen_daily(self) -> Tuple[List[BreakoutCandidate], Dict[str, Any]]:
        """
        Screen for today's best triggers among weekly candidates.
        Ranks by daily trigger score weighted with weekly setup.
        """
        candidates, index_stats = await self.screen()

        ranked = sorted(
            candidates,
            key=lambda c: c.daily_score * 0.6 + c.setup_score * 0.4,
            reverse=True,
        )
        return ranked, index_stats


# Standalone test
async def test_screener():
    import yaml
    import os
    sys_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(sys_path, "config", "settings.yaml"), "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    screener = WeeklyScreener(config)
    candidates, index_stats = await screener.screen()
    print(f"Index: {index_stats}")
    for i, c in enumerate(candidates[:10], 1):
        print(f"{i}. {c.symbol} ({c.name}) skor={c.setup_score:.0f} "
              f"[{SETUP_TURKISH.get(c.setup_type, c.setup_type)}] fiyat={c.price:.2f} "
              f"RS={c.rs_4w:+.1f} dist52={c.dist_52w_high_pct:+.1f}% atr={c.atr_pct:.1f}%")
        for r in c.reasons:
            print(f"     - {r}")


if __name__ == "__main__":
    asyncio.run(test_screener())
