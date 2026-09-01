"""Weekly Focus 20 engine for NASDQ13.

The broad mid-cap universe is used only to discover the week's focus list.
After the list is locked, subsequent scans refresh those same symbols instead
of repeatedly re-ranking the full universe. This gives the expensive chart,
news and trigger layers a stable set of names to study deeply all week.

The list is deterministic and explainable. It is not a probability model and
it never bypasses the existing risk/trigger logic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.analysis.smallcap_scanner import SmallCapCandidate


class WeeklyFocusEngine:
    """Create and maintain a fixed ISO-week focus list of up to 20 stocks."""

    VERSION = "weekly-focus-v1"
    DEFAULT_SIZE = 20

    def __init__(self, state: Optional[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> None:
        self.state = state if state is not None else {}
        self.config = config or {}
        raw = ((self.config.get("smallcap", {}) or {}).get("weekly_focus", {}) or {})
        self.enabled = bool(raw.get("enabled", True))
        self.size = max(5, min(30, int(raw.get("size", self.DEFAULT_SIZE))))

    @staticmethod
    def week_key(now: Optional[datetime] = None) -> str:
        dt = now or datetime.now(timezone.utc)
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        try:
            v = float(value)
            if v != v:
                return default
            return v
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
        return float(np.clip(value, low, high))

    def current_record(self) -> Optional[Dict[str, Any]]:
        rec = self.state.get("weekly_focus")
        if not isinstance(rec, dict):
            return None
        if rec.get("week_key") != self.week_key():
            return None
        if not rec.get("items"):
            return None
        return rec

    def has_current_week(self) -> bool:
        return self.current_record() is not None

    @classmethod
    def selection_score(cls, c: SmallCapCandidate) -> float:
        """Technical readiness score used only to choose the fixed weekly 20."""
        setup = cls._num(c.setup_score)
        anticipation = cls._num(c.anticipation_score)
        rs = cls._num(c.rs_4w)
        dist = cls._num(c.dist_to_resistance_pct, 99.0)
        squeeze = int(cls._num(c.squeeze_days))
        atr_contract = cls._num(c.atr_contraction_pct)
        rvol = cls._num(c.vol_ratio, 1.0)
        pattern_bias = int(cls._num(c.pattern_bias))

        # Sweet spot: close enough to resistance to matter, but not already
        # excessively extended above it.
        if -0.5 <= dist <= 1.5:
            location = 100.0
        elif 1.5 < dist <= 4.0:
            location = 82.0
        elif 4.0 < dist <= 8.0:
            location = 58.0
        elif -2.0 <= dist < -0.5:
            location = 48.0
        else:
            location = 25.0

        rs_score = cls._clip(50.0 + rs * 5.0)
        squeeze_score = cls._clip(squeeze * 8.0)
        contraction_score = cls._clip((-atr_contract) * 4.0) if atr_contract < 0 else 25.0
        volume_score = 72.0 if 0.7 <= rvol <= 1.4 else 88.0 if 1.4 < rvol <= 2.5 else 45.0
        pattern_score = 75.0 if pattern_bias > 0 else 45.0 if pattern_bias < 0 else 60.0
        nr7_score = 100.0 if bool(c.is_nr7) else 55.0

        score = (
            setup * 0.30
            + anticipation * 0.25
            + location * 0.12
            + rs_score * 0.10
            + squeeze_score * 0.08
            + contraction_score * 0.05
            + volume_score * 0.04
            + pattern_score * 0.03
            + nr7_score * 0.03
        )
        return round(cls._clip(score), 1)

    @classmethod
    def readiness_status(cls, row: Dict[str, Any]) -> str:
        price = cls._num(row.get("price"))
        support = cls._num(row.get("support_pivot"))
        setup = cls._num(row.get("setup_score"))
        anticipation = cls._num(row.get("anticipation_score"))
        dist = cls._num(row.get("dist_to_resistance_pct"), 99.0)
        trigger_score = cls._num(row.get("trigger_score"))
        trigger = str(row.get("trigger_type") or "none").lower()

        if support > 0 and price > 0 and price < support * 0.985:
            return "FAILED"
        if trigger == "breakout" and trigger_score >= 60:
            return "TRIGGERED"
        if (anticipation >= 72 and -1.0 <= dist <= 2.5) or (setup >= 72 and 0 <= dist <= 1.5):
            return "BREAKOUT_READY"
        if setup >= 68 or anticipation >= 65:
            return "SETUP"
        if setup >= 50 or anticipation >= 50:
            return "WATCH"
        return "COLD"

    @classmethod
    def live_focus_score(cls, row: Dict[str, Any]) -> float:
        setup = cls._num(row.get("setup_score"))
        anticipation = cls._num(row.get("anticipation_score"))
        trigger = cls._num(row.get("trigger_score"))
        base = cls._num(row.get("weekly_selection_score"), setup)
        score = base * 0.25 + setup * 0.30 + anticipation * 0.30 + trigger * 0.15
        status = cls.readiness_status(row)
        if status == "TRIGGERED":
            score += 8
        elif status == "BREAKOUT_READY":
            score += 4
        elif status == "FAILED":
            score -= 15
        return round(cls._clip(score), 1)

    @staticmethod
    def _snapshot(c: SmallCapCandidate, selection_score: float, rank: int) -> Dict[str, Any]:
        data = c.to_dict()
        data.update({
            "weekly_rank": rank,
            "weekly_selection_score": selection_score,
            "weekly_status": WeeklyFocusEngine.readiness_status(data),
        })
        return data

    def create_week(
        self,
        candidates: Sequence[SmallCapCandidate],
        universe: Sequence[Dict[str, Any]],
    ) -> Tuple[List[SmallCapCandidate], Dict[str, Any]]:
        scored = sorted(
            [(self.selection_score(c), c) for c in candidates],
            key=lambda pair: (pair[0], pair[1].setup_score, pair[1].anticipation_score),
            reverse=True,
        )[: self.size]

        selected: List[SmallCapCandidate] = []
        items: List[Dict[str, Any]] = []
        for rank, (score, cand) in enumerate(scored, start=1):
            cand.weekly_selection_score = score
            cand.weekly_rank = rank
            selected.append(cand)
            items.append(self._snapshot(cand, score, rank))

        minimal_universe = [
            {
                "symbol": u.get("symbol"),
                "name": u.get("name", u.get("symbol")),
                "market_cap": u.get("market_cap", 0),
            }
            for u in universe
            if isinstance(u, dict) and u.get("symbol")
        ]
        now = datetime.now(timezone.utc).isoformat()
        rec = {
            "version": self.VERSION,
            "week_key": self.week_key(),
            "selected_at": now,
            "last_refresh_at": now,
            "size": len(items),
            "universe_size": len(minimal_universe),
            "symbols": [x["symbol"] for x in items],
            "items": items,
            "universe": minimal_universe,
            "locked_for_week": True,
        }
        self.state["weekly_focus"] = rec
        return selected, rec

    @staticmethod
    def _candidate_from_parts(
        symbol: str,
        name: str,
        market_cap: float,
        snap: Any,
        score: float,
        stype: str,
        reasons: List[str],
        detail: Dict[str, Any],
    ) -> SmallCapCandidate:
        return SmallCapCandidate(
            symbol=symbol,
            name=name,
            price=snap.price,
            change_pct=snap.change_pct,
            market_cap=market_cap,
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
            anticipation_score=detail["anticipation_score"],
            dist_to_resistance_pct=detail["dist_to_resistance_pct"],
            bbw_slope_pct=detail["bbw_slope_pct"],
            squeeze_days=detail["squeeze_days"],
            atr_contraction_pct=detail["atr_contraction_pct"],
            expect_horizon=detail["expect_horizon"],
            candle_patterns=detail["candle_patterns"],
            pattern_bias=detail["pattern_bias"],
            pattern_bonus=detail["pattern_bonus"],
            resistance_pivot=detail["resistance_pivot"],
            support_pivot=detail["support_pivot"],
            dist_to_pivot_res_pct=detail["dist_to_pivot_res_pct"],
            dist_to_pivot_sup_pct=detail["dist_to_pivot_sup_pct"],
            is_nr7=detail["is_nr7"],
            session_vwap=detail["session_vwap"],
        )

    async def refresh_focus(self, scanner: Any) -> Tuple[List[SmallCapCandidate], List[Dict[str, Any]]]:
        """Refresh only the locked 20 names using daily data.

        Unlike the broad discovery scan, a focus stock is not dropped merely
        because its setup score moved below the normal discovery threshold.
        """
        rec = self.current_record()
        if not rec:
            return [], []

        items = rec.get("items", []) or []
        symbols = [str(x.get("symbol")) for x in items if x.get("symbol")]
        if not symbols:
            return [], rec.get("universe", []) or []

        # Index context for the same RS calculation used by the canonical scanner.
        idx_ret_21 = 0.0
        try:
            idx_data = await scanner._fetch_timeframes([scanner.index_symbol], ["1d"])
            idx_obj = idx_data.get(scanner.index_symbol, {}).get("1d")
            if idx_obj is not None and idx_obj.data is not None and len(idx_obj.data) >= 21:
                close = idx_obj.data["close"]
                idx_ret_21 = float(close.iloc[-1] / close.iloc[-21] - 1) * 100
        except Exception:
            idx_ret_21 = 0.0

        fetched = await scanner._fetch_timeframes(symbols, ["1d"])
        item_by_symbol = {str(x.get("symbol")): x for x in items}
        universe_by_symbol = {
            str(x.get("symbol")): x for x in (rec.get("universe", []) or []) if x.get("symbol")
        }
        refreshed: List[SmallCapCandidate] = []

        for symbol in symbols:
            pd_obj = fetched.get(symbol, {}).get("1d")
            if pd_obj is None or pd_obj.data is None or len(pd_obj.data) < 60:
                continue
            try:
                snap = scanner.analyzer.analyze(pd_obj.data, symbol, "1d")
                if snap is None:
                    continue
                score, stype, reasons, detail = scanner._daily_setup(symbol, snap, pd_obj.data, idx_ret_21)
                meta = universe_by_symbol.get(symbol, {})
                old = item_by_symbol.get(symbol, {})
                cand = self._candidate_from_parts(
                    symbol=symbol,
                    name=str(meta.get("name") or old.get("name") or symbol),
                    market_cap=self._num(meta.get("market_cap"), self._num(old.get("market_cap"))),
                    snap=snap,
                    score=score,
                    stype=stype,
                    reasons=reasons,
                    detail=detail,
                )
                cand.weekly_rank = int(old.get("weekly_rank") or 999)
                cand.weekly_selection_score = self._num(old.get("weekly_selection_score"), score)
                refreshed.append(cand)
            except Exception:
                continue

        # Keep the original 20 symbols fixed, but let current readiness order the
        # screen inside the week. Original weekly rank is used as a stable tie-break.
        refreshed.sort(
            key=lambda c: (
                self.live_focus_score({**c.to_dict(), "weekly_selection_score": getattr(c, "weekly_selection_score", c.setup_score)}),
                -int(getattr(c, "weekly_rank", 999)),
            ),
            reverse=True,
        )
        rec["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
        return refreshed, rec.get("universe", []) or []

    def update_record_from_live(self, candidates: Sequence[SmallCapCandidate]) -> Optional[Dict[str, Any]]:
        rec = self.current_record()
        if not rec:
            return None
        old_by_symbol = {str(x.get("symbol")): x for x in (rec.get("items", []) or [])}
        new_items: List[Dict[str, Any]] = []
        for cand in candidates:
            old = old_by_symbol.get(cand.symbol, {})
            row = cand.to_dict()
            row["weekly_rank"] = int(old.get("weekly_rank") or getattr(cand, "weekly_rank", 999))
            row["weekly_selection_score"] = self._num(
                old.get("weekly_selection_score"),
                getattr(cand, "weekly_selection_score", cand.setup_score),
            )
            row["weekly_status"] = self.readiness_status(row)
            row["weekly_live_score"] = self.live_focus_score(row)
            new_items.append(row)
        new_items.sort(key=lambda x: x.get("weekly_live_score", 0), reverse=True)
        rec["items"] = new_items
        rec["symbols"] = [x.get("symbol") for x in new_items]
        rec["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
        self.state["weekly_focus"] = rec
        return rec

    def dashboard_payload(
        self,
        current_candidates: Optional[Sequence[Dict[str, Any]]] = None,
        news: Optional[Dict[str, Dict[str, Any]]] = None,
        chart_analyses: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        rec = self.current_record()
        if not rec:
            return {
                "active": False,
                "week_key": self.week_key(),
                "message": "Bu hafta için Focus 20 listesi henüz oluşturulmadı.",
                "items": [],
            }

        current = {
            str(x.get("symbol") or "").upper(): x
            for x in (current_candidates or [])
            if isinstance(x, dict) and x.get("symbol")
        }
        news = news or {}
        chart_analyses = chart_analyses or {}
        items: List[Dict[str, Any]] = []
        for stored in rec.get("items", []) or []:
            symbol = str(stored.get("symbol") or "").upper()
            row = dict(stored)
            if symbol in current:
                row.update(current[symbol])
            aggregate = news.get(symbol) or {}
            if aggregate:
                row["news_sentiment_score"] = aggregate.get("score")
                row["news_article_count"] = aggregate.get("article_count")
            chart = chart_analyses.get(symbol) or {}
            if chart:
                row["chart_comment"] = chart.get("comment")
                row["chart_provider"] = chart.get("provider")
            row["weekly_status"] = self.readiness_status(row)
            row["weekly_live_score"] = self.live_focus_score(row)
            items.append(row)

        items.sort(key=lambda x: x.get("weekly_live_score", 0), reverse=True)
        counts: Dict[str, int] = {}
        for row in items:
            status = row.get("weekly_status", "WATCH")
            counts[status] = counts.get(status, 0) + 1
        return {
            "active": True,
            "version": rec.get("version"),
            "week_key": rec.get("week_key"),
            "selected_at": rec.get("selected_at"),
            "last_refresh_at": rec.get("last_refresh_at"),
            "locked_for_week": True,
            "universe_size": rec.get("universe_size", 0),
            "focus_size": len(items),
            "status_counts": counts,
            "items": items,
            "note": "250'lik evren haftalık keşif içindir; hafta içinde derin takip bu sabit Focus 20 üzerinde yapılır.",
        }
