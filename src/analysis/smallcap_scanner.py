"""
Small-Cap Scanner
- Ranks the auto-discovered small-cap universe by breakout setup strength (daily)
- Scans a short watchlist on 15m intraday data for actual breakout triggers
- No API key needed (universe comes from screener_fetcher, prices from yfinance)
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

import numpy as np
import pandas as pd

from src.utils.logger import get_logger
from src.data.price_fetcher import PriceFetcher
from src.data.screener_fetcher import UniverseFetcher
from src.analysis.technical import TechnicalAnalyzer, IndicatorSnapshot

logger = get_logger(__name__)


@dataclass
class SmallCapCandidate:
    """A small-cap ranked by its breakout setup and current trigger state"""
    symbol: str
    name: str
    price: float
    change_pct: float
    market_cap: float
    setup_score: float = 0.0            # 0-100 daily breakout setup
    setup_type: str = "watch"
    reasons: List[str] = field(default_factory=list)

    # Setup detail (daily)
    rsi_14: float = 50.0
    bb_width_percentile: float = 50.0
    dist_52w_high_pct: float = 0.0
    vol_ratio: float = 1.0
    rs_4w: float = 0.0
    donchian_upper: float = 0.0
    atr_pct: float = 0.0

    # Trigger detail (15m)
    trigger_score: float = 0.0
    trigger_type: Optional[str] = None   # breakout | near | none
    trigger_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": round(self.price, 2),
            "change_pct": round(self.change_pct, 2),
            "market_cap": round(self.market_cap, 0),
            "setup_score": round(self.setup_score, 1),
            "setup_type": self.setup_type,
            "reasons": self.reasons,
            "rsi_14": round(self.rsi_14, 1),
            "bb_width_percentile": round(self.bb_width_percentile, 1),
            "dist_52w_high_pct": round(self.dist_52w_high_pct, 2),
            "vol_ratio": round(self.vol_ratio, 2),
            "rs_4w": round(self.rs_4w, 2),
            "donchian_upper": round(self.donchian_upper, 2),
            "atr_pct": round(self.atr_pct, 2),
            "trigger_score": round(self.trigger_score, 1),
            "trigger_type": self.trigger_type,
            "trigger_reasons": self.trigger_reasons,
        }


SETUP_TR = {
    "squeeze": "Sıkışma (Squeeze)",
    "basing": "Zirveye Dayalı",
    "trend": "Güçlü Trend",
    "momentum": "Momentum",
    "watch": "İzleme",
}


class SmallCapScanner:
    """Rule-based daily setup scorer + intraday trigger scanner for small caps"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.sc = config.get("smallcap", {})

        self.watchlist_size = int(self.sc.get("watchlist_size", 25))
        self.min_setup_score = float(self.sc.get("min_setup_score", 55))
        self.top_n_report = int(self.sc.get("top_n_report", 10))
        self.lookback_days = int(self.sc.get("lookback_days", 260))
        self.index_symbol = self.sc.get("index_symbol", "^GSPC")

        self.analyzer = TechnicalAnalyzer(config)
        self.universe_fetcher = UniverseFetcher(config)

        self._symbol_names: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def _fetch_config(self, symbols: List[str], timeframes: List[str]) -> Dict[str, Any]:
        sc_cfg = self.sc
        return {
            "symbols": symbols,
            "timeframes": timeframes,
            "scanner": {
                "max_concurrent_requests": int(sc_cfg.get("max_concurrent_requests", 6)),
                "request_timeout": int(sc_cfg.get("request_timeout", 60)),
            },
        }

    async def _fetch_timeframes(self, symbols: List[str], timeframes: List[str]) -> Dict[str, Dict[str, Any]]:
        fetcher = PriceFetcher(self._fetch_config(symbols, timeframes))
        return await fetcher.fetch_all()

    # ------------------------------------------------------------------
    # Daily setup scoring
    # ------------------------------------------------------------------

    def _rs_vs_index(self, daily_df: pd.DataFrame, close: pd.Series) -> float:
        if len(daily_df) < 22:
            return 0.0
        ret_stock = float(close.iloc[-1] / close.iloc[-21] - 1) * 100
        return ret_stock

    def _daily_setup(self, symbol: str, snap: IndicatorSnapshot, df: pd.DataFrame,
                     idx_ret_21: float) -> Tuple[float, str, List[str], Dict[str, Any]]:
        close = df["close"]
        high = df["high"]

        # Bollinger width percentile (daily) - lower = tighter squeeze
        bb_period = self.analyzer.bb_period
        mid = close.rolling(window=bb_period).mean()
        std = close.rolling(window=bb_period).std()
        width = (2 * self.analyzer.bb_std * std) / mid * 100
        width = width.replace([np.inf, -np.inf], np.nan).dropna()
        bbw = float(width.iloc[-1]) if len(width) else 0.0
        bbw_pctile = float((width.rank(pct=True)).iloc[-1] * 100) if len(width) > 20 else 50.0

        # 52-week high distance
        n52 = min(252, len(close))
        high_52 = float(high.tail(n52).max())
        dist_52 = (snap.price / high_52 - 1) * 100 if high_52 > 0 else 0.0

        # Relative strength vs index (last ~21 sessions)
        ret_stock = float(close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) >= 21 else 0.0
        rs_4w = ret_stock - idx_ret_21

        score = 0.0
        reasons: List[str] = []

        # 1) Squeeze / volatility compression (0-30)
        if bbw_pctile < 25:
            score += (25 - bbw_pctile) * (30 / 25)
            reasons.append(f"📐 Bollinger sıkışması (son bazda %{bbw_pctile:.0f})")
        elif bbw_pctile < 50:
            score += 10 * (1 - bbw_pctile / 50)

        # 2) Proximity to 52-week high (0-25)
        if dist_52 <= 0:
            score += 25
            reasons.append("🎯 52 haftalık zirvede / zirveye dayanmış")
        elif dist_52 <= 3:
            score += 20
            reasons.append(f"🎯 52H zirvenin %{dist_52:.1f} altında")
        elif dist_52 <= 7:
            score += 12
        elif dist_52 <= 12:
            score += 5

        # 3) Relative strength vs index (0-20)
        score += float(np.clip(5 + rs_4w * 1.5, 0, 20))
        if rs_4w > 3:
            reasons.append(f"⚡ S&P 500'e göre +{rs_4w:.1f} puan güçlü")

        # 4) Trend alignment (0-15)
        ema_ok = snap.price > snap.ema_21 > snap.ema_50
        if ema_ok:
            score += 12
            reasons.append("📈 Fiyat > EMA21 > EMA50")
        elif snap.price > snap.ema_21:
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
        if 55 <= snap.rsi_14 <= 72:
            reasons.append(f"🔥 RSI {snap.rsi_14:.0f}")

        # 6) Volume behavior (0-10)
        if snap.volume_ratio >= 1.5:
            score += 8
            reasons.append(f"🔊 Hacim artışı ({snap.volume_ratio:.1f}x)")
        elif snap.volume_ratio >= 1.2:
            score += 4
        elif snap.volume_ratio < 0.8:
            score += 3
            reasons.append("🔇 Hacim daralması (birikim)")

        score = float(np.clip(score, 0, 100))
        stype = "watch"
        if bbw_pctile < 25 and abs(snap.change_pct) < 8:
            stype = "squeeze"
        elif dist_52 <= 0:
            stype = "basing"
        elif ema_ok and rs_4w > 0:
            stype = "trend"
        elif snap.macd_hist > 0 and 50 <= snap.rsi_14 <= 72:
            stype = "momentum"

        detail = {
            "rsi_14": snap.rsi_14,
            "bb_width_percentile": bbw_pctile,
            "dist_52w_high_pct": dist_52,
            "vol_ratio": snap.volume_ratio,
            "rs_4w": rs_4w,
            "atr_pct": (snap.atr_14 / snap.price * 100) if snap.price > 0 and snap.atr_14 > 0 else 0.0,
        }
        return score, stype, reasons, detail

    async def screen_setups(self, force_universe: bool = False) -> Tuple[List[SmallCapCandidate], List[Dict[str, Any]]]:
        """Rank the small-cap universe by daily breakout-setup score.

        Returns (ranked candidates, universe list).
        """
        universe = await self.universe_fetcher.fetch_universe(force=force_universe)
        symbols = [u["symbol"] for u in universe]
        for u in universe:
            self._symbol_names[u["symbol"]] = u["name"]
        if not symbols:
            return [], universe

        # Index context (last 21-day return)
        idx_ret_21 = 0.0
        try:
            idx_data = await self._fetch_timeframes([self.index_symbol], ["1d"])
            idx_df = idx_data.get(self.index_symbol, {}).get("1d")
            if idx_df is not None and len(idx_df.data) >= 21:
                c = idx_df.data["close"]
                idx_ret_21 = float(c.iloc[-1] / c.iloc[-21] - 1) * 100
        except Exception as e:
            logger.warning(f"Index fetch failed for RS: {e}")

        # Fetch daily data for the universe in one go
        fetched = await self._fetch_timeframes(symbols, ["1d"])

        candidates: List[SmallCapCandidate] = []
        for u in universe:
            symbol = u["symbol"]
            pd_obj = fetched.get(symbol, {}).get("1d")
            if pd_obj is None or pd_obj.data is None or len(pd_obj.data) < 60:
                continue
            try:
                snap = self.analyzer.analyze(pd_obj.data, symbol, "1d")
                if snap is None:
                    continue
                score, stype, reasons, detail = self._daily_setup(symbol, snap, pd_obj.data, idx_ret_21)
                if score < self.min_setup_score:
                    continue
                candidates.append(SmallCapCandidate(
                    symbol=symbol,
                    name=self._symbol_names.get(symbol, symbol),
                    price=snap.price,
                    change_pct=snap.change_pct,
                    market_cap=float(u.get("market_cap", 0)),
                    setup_score=score,
                    setup_type=stype,
                    reasons=reasons,
                    rsi_14=detail["rsi_14"],
                    bb_width_percentile=detail["bb_width_percentile"],
                    dist_52w_high_pct=detail["dist_52w_high_pct"],
                    vol_ratio=detail["vol_ratio"],
                    rs_4w=detail["rs_4w"],
                    donchian_upper=snap.donchian_upper_20,
                    atr_pct=detail["atr_pct"],
                ))
            except Exception as e:
                logger.warning(f"Setup error {symbol}: {e}")

        candidates.sort(key=lambda c: c.setup_score, reverse=True)
        logger.info(f"SmallCap setups: {len(universe)} tarandı, {len(candidates)} aday")
        return candidates, universe

    # ------------------------------------------------------------------
    # Intraday trigger scoring (15m)
    # ------------------------------------------------------------------

    def _intraday_trigger(self, cand: SmallCapCandidate, snap: IndicatorSnapshot) -> Tuple[float, Optional[str], List[str]]:
        """Score the current 15m breakout trigger against the daily setup level."""
        score = 0.0
        reasons: List[str] = []
        trig = "none"

        vr = snap.volume_ratio
        gap = snap.gap_pct

        # Above daily Donchian upper band -> actual breakout
        if cand.donchian_upper > 0:
            if snap.price > cand.donchian_upper:
                score += 40
                trig = "breakout"
                reasons.append(f"📈 Günlük direnç ({cand.donchian_upper:.2f}) kırıldı")
            elif snap.price >= cand.donchian_upper * 0.99:
                score += 20
                trig = "near"
                reasons.append(f"🎯 Günlük dirence dayanıyor ({cand.donchian_upper:.2f})")

        if snap.is_breakout_up or (snap.price > snap.bb_upper):
            score += 15
            if trig == "none":
                trig = "breakout"
            reasons.append("📊 15m Donchian/BB üst band kırılımı")

        if vr >= 2.0:
            score += 20
            reasons.append(f"🔊 Hacim patlaması ({vr:.1f}x)")
        elif vr >= 1.5:
            score += 12
            reasons.append(f"🔊 Hacim artışı ({vr:.1f}x)")

        if 55 <= snap.rsi_14 <= 78:
            score += 10
        if snap.macd_hist > 0 and snap.macd > snap.macd_signal:
            score += 10
            reasons.append("💹 MACD pozitif")
        if snap.change_pct > 2.0:
            score += 10
            reasons.append(f"🚀 15m {snap.change_pct:+.1f}%")
        if gap > 0.5:
            score += 8
            reasons.append("🌅 Gap up")

        # Weight by setup strength (a strong setup + trigger = best signal)
        final = score * 0.5 + cand.setup_score * 0.5
        if trig == "none" and final < 45:
            reasons = []
        return float(np.clip(final, 0, 100)), trig, reasons

    async def scan_triggers(self, candidates: List[SmallCapCandidate],
                            universe: List[Dict[str, Any]] = None) -> Tuple[List[SmallCapCandidate], List[str]]:
        """Scan the top watchlist for intraday breakout triggers on 15m data.

        Returns (updated candidates with trigger info, triggered symbol list).
        """
        watch = candidates[: self.watchlist_size]
        if not watch:
            return candidates, []
        symbols = [c.symbol for c in watch]
        for u in (universe or []):
            self._symbol_names[u["symbol"]] = u["name"]

        fetched = await self._fetch_timeframes(symbols, ["15m"])

        triggered: List[str] = []
        for cand in watch:
            pd_obj = fetched.get(cand.symbol, {}).get("15m")
            if pd_obj is None or pd_obj.data is None or len(pd_obj.data) < 40:
                continue
            try:
                snap = self.analyzer.analyze(pd_obj.data, cand.symbol, "15m")
                if snap is None:
                    continue
                score, trig, reasons = self._intraday_trigger(cand, snap)
                cand.trigger_score = score
                cand.trigger_type = trig
                cand.trigger_reasons = reasons
                if trig == "breakout":
                    triggered.append(cand.symbol)
            except Exception as e:
                logger.warning(f"Trigger error {cand.symbol}: {e}")

        watch.sort(key=lambda c: c.trigger_score, reverse=True)
        logger.info(f"SmallCap triggers: {len(watch)} izlendi, breakout={len(triggered)}")
        return watch, triggered

    # ------------------------------------------------------------------
    # Report builders (Turkish)
    # ------------------------------------------------------------------

    def build_setup_report(self, candidates: List[SmallCapCandidate], universe_size: int) -> str:
        """Build a Turkish setup report message (top scorers)."""
        from src.utils.timezone import now_turkey

        now = now_turkey()
        lines = [
            "🎯 <b>MID-CAP ÇIKIŞ ADAYLARI</b>",
            f"📅 {now.strftime('%d.%m.%Y %H:%M')} · {universe_size} hisse tarandı",
            "─" * 30,
        ]
        top = candidates[: self.top_n_report]
        if not top:
            lines.append("Şu an eşiği aşan aday yok.")
            return "\n".join(lines)

        for i, c in enumerate(top, 1):
            lines.append(
                f"🏆 {i}. <b>{c.name}</b> (<code>{c.symbol}</code>) — setup <b>{c.setup_score:.0f}</b> [{SETUP_TR.get(c.setup_type, c.setup_type)}]\n"
                f"   💰 {c.price:,.2f} USD | Cap: {c.market_cap/1e9:.1f}B | RSI: {c.rsi_14:.0f} | 52H: {c.dist_52w_high_pct:+.1f}%"
            )
            lines.append("")
        lines.append("⚠️ <i>Otomatik üretilmiştir, yatırım tavsiyesi değildir.</i>")
        return "\n".join(lines)

    def build_predictions_report(self, candidates: List[SmallCapCandidate], universe_size: int) -> str:
        """Build a Turkish "tomorrow breakout predictions" report for when the
        market is closed. Uses daily setup scores + resistance levels to flag
        names likely to move at the next open."""
        from src.utils.timezone import now_turkey

        now = now_turkey()
        lines = [
            "📅 <b>YARIN İÇİN MID-CAP ÇIKIŞ TAHMİNLERİ</b>",
            f"🕐 {now.strftime('%d.%m.%Y %H:%M')} · {universe_size} hisse tarandı · piyasa kapalı",
            "─" * 30,
        ]
        top = candidates[: self.top_n_report]
        if not top:
            lines.append("Şu an yarın için net aday yok; tarama sürüyor.")
            return "\n".join(lines)

        for i, c in enumerate(top, 1):
            # Kırılması gereken seviye: günlük resistance (Donchian üst)
            level = c.donchian_upper if c.donchian_upper > 0 else c.price
            dist_pct = (level / c.price - 1) * 100 if c.price > 0 else 0.0
            lines.append(
                f"{i}. <b>{c.name}</b> (<code>{c.symbol}</code>) — <b>{c.setup_score:.0f}</b> [{SETUP_TR.get(c.setup_type, c.setup_type)}]\n"
                f"   💰 {c.price:,.2f} USD | Cap: {c.market_cap/1e9:.1f}B\n"
                f"   🔓 Kırılması gereken: <b>{level:,.2f} USD</b> (+{dist_pct:.1f}%) | ATR: ±{c.atr_pct:.1f}%\n"
                f"   🔍 RSI: {c.rsi_14:.0f} | 52H: {c.dist_52w_high_pct:+.1f}% | Hacim: {c.vol_ratio:.1f}x"
            )
            lines.append("")
        lines.append("💡 <b>Tahmin:</b> bu hisseler setup'a yakın; kırılım anlık alarm ile bildirilir.")
        lines.append("⚠️ <i>Otomatik üretilmiştir, yatırım tavsiyesi değildir.</i>")
        return "\n".join(lines)

    def build_trigger_message(self, c: SmallCapCandidate) -> str:
        """Build a Turkish breakout-trigger alarm message."""
        from src.utils.timezone import now_turkey

        now = now_turkey()
        tv = f"https://www.tradingview.com/chart/?symbol={c.symbol}"
        lines = [
            "🚨 <b>MID-CAP BREAKOUT ALARMI</b>",
            "─" * 30,
            f"🏷 <b>{c.name}</b> (<code>{c.symbol}</code>)",
            f"💰 <b>{c.price:,.2f} USD</b> | {c.change_pct:+.2f}%",
            f"⚡ <b>{SETUP_TR.get(c.setup_type, c.setup_type)}</b> (setup {c.setup_score:.0f}/100)",
            f"🔔 <b>Tetik:</b> {c.trigger_score:.0f}/100",
        ]
        if c.trigger_reasons:
            lines.append("")
            lines.append("🔍 " + " | ".join(c.trigger_reasons[:4]))
        lines.append("")
        lines.append(f"📈 <a href='{tv}'>TradingView Grafiğini Aç</a>")
        lines.append(f"🕐 {now.strftime('%d.%m.%Y %H:%M')}")
        lines.append("⚠️ <i>Yatırım tavsiyesi değildir.</i>")
        return "\n".join(lines)


# Standalone test
async def test_smallcap_scanner():
    import os
    import yaml

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "config", "settings.yaml"), "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    scanner = SmallCapScanner(config)
    try:
        candidates, universe = await scanner.screen_setups(force_universe=True)
        print(f"Universe: {len(universe)}, candidates: {len(candidates)}")
        for c in candidates[:10]:
            print(f"  {c.symbol} setup={c.setup_score:.0f} [{c.setup_type}] price={c.price:.2f} RS={c.rs_4w:+.1f}")
        watch, trig = await scanner.scan_triggers(candidates, universe)
        print(f"Triggered: {trig}")
        for c in watch[:5]:
            print(f"  TRIG {c.symbol} score={c.trigger_score:.0f} type={c.trigger_type} reasons={c.trigger_reasons}")
    finally:
        await scanner.universe_fetcher.close()


if __name__ == "__main__":
    asyncio.run(test_smallcap_scanner())