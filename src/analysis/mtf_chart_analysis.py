"""Top-down multi-timeframe chart analysis for the Railway dashboard.

The reader combines independent 1D, 1H and 15M chart snapshots without using
future outcomes or mutating the scanner/ranking strategy. The hierarchy is:
1D market regime -> 1H structure/setup -> 15M execution trigger.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np

from src.analysis.chart_analysis import ChartAnalysisService


class MultiTimeframeChartAnalysisService:
    """Combine three deterministic chart readers into one top-down decision."""

    TIMEFRAMES = ("1d", "1h", "15m")
    WEIGHTS = {"1d": 0.45, "1h": 0.35, "15m": 0.20}
    BULLISH_TRENDS = {"STRONG_BULLISH", "BULLISH"}
    VALID_LONG_SETUPS = {
        "BREAKOUT_CONFIRMED",
        "BREAKOUT_READY",
        "SUPPORT_BOUNCE",
        "TREND_PULLBACK",
    }

    def __init__(self) -> None:
        # Separate readers allow the three Yahoo requests to run concurrently.
        # Each reader still keeps its own short TTL cache.
        self._readers = {
            timeframe: ChartAnalysisService(cache_ttl_seconds=60)
            for timeframe in self.TIMEFRAMES
        }

    async def analyze(self, symbol: str) -> Dict[str, Any]:
        results = await asyncio.gather(
            *[self._readers[tf].analyze(symbol=symbol, timeframe=tf) for tf in self.TIMEFRAMES]
        )
        frames = {tf: payload for tf, payload in zip(self.TIMEFRAMES, results)}
        return self._combine(symbol=symbol.upper().strip(), frames=frames)

    @staticmethod
    def _snapshot(frame: Dict[str, Any]) -> Dict[str, Any]:
        return frame.get("snapshot") or {}

    @staticmethod
    def _frame_summary(frame: Dict[str, Any]) -> Dict[str, Any]:
        snap = frame.get("snapshot") or {}
        plan = frame.get("trade_plan") or {}
        return {
            "timeframe": frame.get("timeframe"),
            "price": snap.get("price"),
            "trend": snap.get("trend"),
            "market_structure": snap.get("market_structure"),
            "setup": snap.get("setup"),
            "decision": snap.get("decision"),
            "score": snap.get("score"),
            "rsi14": snap.get("rsi14"),
            "rvol": snap.get("rvol"),
            "support": snap.get("support"),
            "resistance": snap.get("resistance"),
            "entry": plan.get("entry"),
            "stop": plan.get("stop"),
            "target1": plan.get("target1"),
            "target2": plan.get("target2"),
        }

    def _combine(self, symbol: str, frames: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        daily = self._snapshot(frames["1d"])
        hourly = self._snapshot(frames["1h"])
        trigger = self._snapshot(frames["15m"])

        d_trend = daily.get("trend")
        h_trend = hourly.get("trend")
        m_trend = trigger.get("trend")
        d_structure = daily.get("market_structure")
        h_structure = hourly.get("market_structure")
        m_setup = trigger.get("setup")

        daily_bull = d_trend in self.BULLISH_TRENDS
        hourly_bull = h_trend in self.BULLISH_TRENDS
        minute_bull = m_trend in self.BULLISH_TRENDS
        daily_bear = d_trend == "BEARISH"
        hourly_bear = h_trend == "BEARISH"
        trigger_valid = m_setup in self.VALID_LONG_SETUPS and trigger.get("decision") != "AVOID"
        trigger_confirmed = m_setup == "BREAKOUT_CONFIRMED"

        weighted_score = sum(
            float((self._snapshot(frames[tf]).get("score") or 0.0)) * weight
            for tf, weight in self.WEIGHTS.items()
        )

        alignment_bonus = 0.0
        if daily_bull and hourly_bull:
            alignment_bonus += 6.0
        if daily_bull and hourly_bull and minute_bull:
            alignment_bonus += 4.0
        if d_structure == "HH_HL":
            alignment_bonus += 3.0
        if h_structure == "HH_HL":
            alignment_bonus += 3.0
        if trigger_confirmed:
            alignment_bonus += 5.0
        elif trigger_valid:
            alignment_bonus += 2.0
        if daily_bear:
            alignment_bonus -= 14.0
        if hourly_bear:
            alignment_bonus -= 10.0
        if d_trend != h_trend and (daily_bear or hourly_bear):
            alignment_bonus -= 4.0

        mtf_score = round(float(np.clip(weighted_score + alignment_bonus, 0, 100)), 1)

        if daily_bear:
            regime = "RISK_OFF"
        elif daily_bull:
            regime = "LONG_BIAS"
        else:
            regime = "NEUTRAL"

        if hourly_bear:
            setup_state = "INVALID"
        elif hourly_bull and h_structure == "HH_HL":
            setup_state = "READY"
        elif hourly_bull or h_structure == "HH_HL":
            setup_state = "FORMING"
        else:
            setup_state = "WAIT"

        if trigger_confirmed:
            trigger_state = "TRIGGERED"
        elif trigger_valid:
            trigger_state = "READY"
        elif trigger.get("decision") == "AVOID":
            trigger_state = "WEAK"
        else:
            trigger_state = "WAIT"

        if daily_bull and hourly_bull and minute_bull:
            alignment = "FULL"
        elif (daily_bear and (hourly_bull or minute_bull)) or (daily_bull and hourly_bear):
            alignment = "CONFLICT"
        elif daily_bull and not hourly_bear:
            alignment = "PARTIAL"
        else:
            alignment = "WEAK"

        # Long-side top-down gate. A high 15M score cannot override a bearish 1D/1H context.
        if daily_bear or hourly_bear:
            decision = "AVOID"
        elif daily_bull and hourly_bull and trigger_confirmed and mtf_score >= 75:
            decision = "STRONG_CANDIDATE"
        elif daily_bull and hourly_bull and trigger_valid and mtf_score >= 65:
            decision = "CANDIDATE"
        elif daily_bull and hourly_bull:
            decision = "WAIT_FOR_TRIGGER"
        elif daily_bull and not hourly_bear:
            decision = "WATCH"
        else:
            decision = "AVOID"

        reasons: List[str] = []
        reasons.append(
            f"1D rejim: {d_trend or '-'} / {d_structure or '-'}; ana yön filtresi "
            f"{'LONG lehine' if daily_bull else 'long için onaysız'}."
        )
        reasons.append(
            f"1H yapı: {h_trend or '-'} / {h_structure or '-'} / {hourly.get('setup') or '-'}; "
            f"setup katmanı {setup_state}."
        )
        reasons.append(
            f"15M tetik: {m_setup or '-'}; RSI {float(trigger.get('rsi14') or 0):.1f}, "
            f"RVOL {float(trigger.get('rvol') or 0):.2f}x; tetik durumu {trigger_state}."
        )
        if alignment == "FULL":
            reasons.append("1D, 1H ve 15M yönleri aynı tarafa hizalanmış; top-down uyum tam.")
        elif alignment == "CONFLICT":
            reasons.append("Zaman dilimleri birbiriyle çelişiyor; düşük zaman dilimi sinyali tek başına yeterli kabul edilmedi.")
        elif alignment == "PARTIAL":
            reasons.append("1D yön olumlu fakat 1H/15M tarafında tam teyit henüz oluşmadı.")
        else:
            reasons.append("Üst zaman dilimi yön avantajı zayıf; yeni long için bekleme/kaçınma filtresi aktif.")
        if decision == "WAIT_FOR_TRIGGER":
            reasons.append("1D ve 1H uygun; işlem adayı olması için 15M breakout/retest/hacim tetik teyidi bekleniyor.")
        elif decision in {"STRONG_CANDIDATE", "CANDIDATE"}:
            reasons.append("Üst zaman dilimi bağlamı ile 15M giriş tetikleyicisi aynı senaryoyu destekliyor.")

        return {
            "symbol": symbol,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "method": {
                "name": "Top-Down MTF Chart Reader v1",
                "hierarchy": ["1D regime", "1H structure/setup", "15M trigger"],
                "weights": self.WEIGHTS,
                "future_outcomes_used": False,
                "mutates_live_ranking": False,
            },
            "summary": {
                "decision": decision,
                "score": mtf_score,
                "alignment": alignment,
                "regime": regime,
                "setup_state": setup_state,
                "trigger_state": trigger_state,
            },
            "timeframes": {
                "1d": self._frame_summary(frames["1d"]),
                "1h": self._frame_summary(frames["1h"]),
                "15m": self._frame_summary(frames["15m"]),
            },
            "explanation": reasons,
        }
