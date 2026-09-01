"""Outcome case analytics for NASDQ13's learning layer.

Reconstructs recommendation cases from persisted ``scan_history`` without
changing live strategy weights. For each regular-session candidate it measures
what happened *after* the original snapshot using later scanner observations:

- scan-observed MFE / MAE,
- approximately one-hour and end-of-day returns,
- +1% / +2% continuation after planned entry,
- target-vs-stop ordering (stop-before-target),
- setup-level performance,
- expanding-window walk-forward diagnostics.

Important limitation: scan history contains snapshot prices, not every intrabar
high/low. Therefore MFE/MAE and hit ordering are deliberately labelled
``scan_observed`` and can understate moves that happened between scans.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


class LearningCaseAnalytics:
    """Build historical learning cases from the bot's persisted scan history."""

    HORIZON_DAYS = 7
    ONE_HOUR_MINUTES = 55
    ONE_HOUR_MAX_MINUTES = 240
    MAX_CASES = 1200

    def __init__(self, state: Optional[Dict[str, Any]]) -> None:
        self.state = state or {}

    @staticmethod
    def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            v = float(value)
            if v != v:  # NaN
                return default
            return v
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pct(current: Optional[float], reference: Optional[float]) -> Optional[float]:
        if current is None or reference is None or reference <= 0:
            return None
        return round((current / reference - 1.0) * 100.0, 3)

    @staticmethod
    def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
        clean = [float(v) for v in values if v is not None]
        if not clean:
            return None
        return round(sum(clean) / len(clean), 3)

    @staticmethod
    def _rate(numerator: int, denominator: int) -> Optional[float]:
        if denominator <= 0:
            return None
        return round(numerator / denominator * 100.0, 1)

    def _rows(self) -> List[Dict[str, Any]]:
        """Flatten scan_history into chronological per-symbol observations."""
        rows: List[Dict[str, Any]] = []
        for scan in self.state.get("scan_history", []) or []:
            ts = self._parse_ts(scan.get("time"))
            if ts is None:
                continue
            closed = bool(scan.get("closed"))
            for candidate in scan.get("candidates", []) or []:
                if not isinstance(candidate, dict):
                    continue
                symbol = str(candidate.get("symbol") or "").upper().strip()
                price = self._num(candidate.get("price"))
                if not symbol or price is None or price <= 0:
                    continue
                rows.append({
                    "ts": ts,
                    "date": ts.date().isoformat(),
                    "closed": closed,
                    "symbol": symbol,
                    "price": price,
                    "candidate": candidate,
                })
        rows.sort(key=lambda r: r["ts"])
        return rows

    @staticmethod
    def _rr(candidate: Dict[str, Any]) -> Optional[float]:
        plan = candidate.get("trade_plan") or {}
        entry = LearningCaseAnalytics._num(plan.get("limit"))
        target = LearningCaseAnalytics._num(plan.get("target"))
        stop = LearningCaseAnalytics._num(plan.get("stop"))
        if not entry or not target or not stop or target <= entry or stop >= entry or stop <= 0:
            return None
        return round((target - entry) / max(entry - stop, 1e-9), 2)

    @classmethod
    def _context_tags(cls, candidate: Dict[str, Any]) -> List[str]:
        """Human-readable context tags used only for descriptive mistake review."""
        tags: List[str] = []
        rvol = cls._num(candidate.get("vol_ratio"), 1.0) or 0.0
        rsi = cls._num(candidate.get("rsi_14"), 50.0) or 50.0
        dist = cls._num(candidate.get("dist_to_resistance_pct"), 99.0)
        setup_score = cls._num(candidate.get("setup_score"), 0.0) or 0.0
        anticipation = cls._num(candidate.get("anticipation_score"), 0.0) or 0.0
        pattern_bias = int(cls._num(candidate.get("pattern_bias"), 0.0) or 0.0)
        trigger = str(candidate.get("trigger_type") or "none").lower()
        news = cls._num(candidate.get("news_score"), 0.0) or 0.0
        squeeze_days = int(cls._num(candidate.get("squeeze_days"), 0.0) or 0.0)
        rr = cls._rr(candidate)

        if rvol < 0.8:
            tags.append("Düşük RVOL (<0.8x)")
        if rvol >= 1.5:
            tags.append("Güçlü RVOL (>=1.5x)")
        if rsi >= 74:
            tags.append("Yüksek RSI (>=74)")
        elif 52 <= rsi <= 69:
            tags.append("Sağlıklı RSI (52-69)")
        if dist is not None and 0 <= dist <= 1.5:
            tags.append("Dirence çok yakın (<=%1.5)")
        if setup_score < 65:
            tags.append("Marjinal setup skoru (<65)")
        if anticipation < 55:
            tags.append("Zayıf öngörü skoru (<55)")
        if pattern_bias < 0:
            tags.append("Bearish mum bias")
        elif pattern_bias > 0:
            tags.append("Bullish mum bias")
        if trigger in {"none", "", "null"}:
            tags.append("15M tetik yok")
        elif trigger == "breakout":
            tags.append("15M breakout tetik")
        elif trigger == "near":
            tags.append("15M kırılıma yakın")
        if news <= -5:
            tags.append("Negatif haber sentimenti")
        if squeeze_days >= 8:
            tags.append("Uzun squeeze (8+ gün)")
        if bool(candidate.get("is_nr7")):
            tags.append("NR7 sıkışması")
        if rr is not None and rr < 1.5:
            tags.append("Planlanan R:R <1.5")
        return tags

    def _origins(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """First regular-session appearance of each symbol per UTC market date."""
        seen = set()
        origins: List[Dict[str, Any]] = []
        for row in rows:
            if row["closed"]:
                continue
            key = (row["symbol"], row["date"])
            if key in seen:
                continue
            seen.add(key)
            origins.append(row)
        return origins[-self.MAX_CASES :]

    @staticmethod
    def _first_at_or_after(
        observations: List[Dict[str, Any]],
        start: datetime,
        min_minutes: float,
        max_minutes: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        for obs in observations:
            minutes = (obs["ts"] - start).total_seconds() / 60.0
            if minutes < min_minutes:
                continue
            if max_minutes is not None and minutes > max_minutes:
                return None
            return obs
        return None

    def _build_case(
        self,
        origin: Dict[str, Any],
        observations: List[Dict[str, Any]],
        history_end: datetime,
    ) -> Dict[str, Any]:
        first_ts = origin["ts"]
        first_price = float(origin["price"])
        candidate = origin["candidate"]
        horizon_end = first_ts + timedelta(days=self.HORIZON_DAYS)
        future = [o for o in observations if first_ts <= o["ts"] <= horizon_end]
        if not future:
            future = [origin]

        same_day = [o for o in future if o["date"] == origin["date"]]
        eod_obs = same_day[-1] if same_day else None
        one_hour_obs = self._first_at_or_after(
            future,
            first_ts,
            self.ONE_HOUR_MINUTES,
            self.ONE_HOUR_MAX_MINUTES,
        )

        prices = [float(o["price"]) for o in future]
        max_price = max(prices)
        min_price = min(prices)

        plan = candidate.get("trade_plan") or {}
        plan_entry = self._num(plan.get("limit"))
        plan_target = self._num(plan.get("target"))
        plan_stop = self._num(plan.get("stop"))
        entry = plan_entry if plan_entry and plan_entry > 0 else first_price
        plan_valid = bool(
            plan_entry and plan_target and plan_stop
            and plan_target > plan_entry
            and 0 < plan_stop < plan_entry
        )

        entry_obs: Optional[Dict[str, Any]] = None
        for obs in future:
            if float(obs["price"]) >= entry:
                entry_obs = obs
                break
        entry_triggered = entry_obs is not None
        entry_ts = entry_obs["ts"] if entry_obs else None

        post_entry = [o for o in future if entry_ts is not None and o["ts"] >= entry_ts]
        post_prices = [float(o["price"]) for o in post_entry]
        post_mfe = self._pct(max(post_prices), entry) if post_prices else None
        post_mae = self._pct(min(post_prices), entry) if post_prices else None

        plus1_obs: Optional[Dict[str, Any]] = None
        plus2_obs: Optional[Dict[str, Any]] = None
        target_obs: Optional[Dict[str, Any]] = None
        stop_obs: Optional[Dict[str, Any]] = None
        if entry_triggered:
            for obs in post_entry:
                price = float(obs["price"])
                if plus1_obs is None and price >= entry * 1.01:
                    plus1_obs = obs
                if plus2_obs is None and price >= entry * 1.02:
                    plus2_obs = obs
                if plan_valid and target_obs is None and price >= float(plan_target):
                    target_obs = obs
                if plan_valid and stop_obs is None and price <= float(plan_stop):
                    stop_obs = obs

        mature = history_end >= horizon_end
        outcome = "TRACKING"
        resolved_at: Optional[datetime] = None
        if plan_valid and entry_triggered:
            if target_obs and stop_obs:
                if target_obs["ts"] <= stop_obs["ts"]:
                    outcome, resolved_at = "TARGET_HIT", target_obs["ts"]
                else:
                    outcome, resolved_at = "STOPPED_OUT", stop_obs["ts"]
            elif target_obs:
                outcome, resolved_at = "TARGET_HIT", target_obs["ts"]
            elif stop_obs:
                outcome, resolved_at = "STOPPED_OUT", stop_obs["ts"]
            elif mature:
                outcome, resolved_at = "EXPIRED", horizon_end
        elif plan_valid and not entry_triggered and mature:
            outcome, resolved_at = "NO_ENTRY", horizon_end
        elif not plan_valid and mature:
            outcome, resolved_at = "OBSERVED_ONLY", horizon_end

        stop_before_target = outcome == "STOPPED_OUT"
        time_to_entry = (
            round((entry_ts - first_ts).total_seconds() / 60.0, 1)
            if entry_ts is not None else None
        )
        time_to_target = (
            round((target_obs["ts"] - entry_ts).total_seconds() / 60.0, 1)
            if target_obs is not None and entry_ts is not None else None
        )
        time_to_stop = (
            round((stop_obs["ts"] - entry_ts).total_seconds() / 60.0, 1)
            if stop_obs is not None and entry_ts is not None else None
        )

        tags = self._context_tags(candidate)
        mistake_flags: List[str] = []
        if outcome in {"STOPPED_OUT", "NO_ENTRY", "EXPIRED"}:
            mistake_flags.extend(tags)
            if outcome == "STOPPED_OUT":
                mistake_flags.append("Stop hedeflerden önce gözlendi")
                if post_mfe is not None and post_mfe >= 1.0:
                    mistake_flags.append("Önce +%1 MFE gördü, sonra stop oldu")
            if outcome == "NO_ENTRY":
                mistake_flags.append("7 günlük ufukta planlı giriş tetiklenmedi")
            if post_mfe is not None and post_mfe < 0.5 and entry_triggered:
                mistake_flags.append("Giriş sonrası takip hareketi zayıf (<%0.5 MFE)")

        return {
            "case_id": f"{origin['date']}:{origin['symbol']}",
            "symbol": origin["symbol"],
            "case_date": origin["date"],
            "first_seen": first_ts.isoformat(),
            "resolved_at": resolved_at.isoformat() if resolved_at else None,
            "source": "regular_scan",
            "setup_type": candidate.get("setup_type") or "unknown",
            "setup_score": self._num(candidate.get("setup_score"), 0.0),
            "anticipation_score": self._num(candidate.get("anticipation_score"), 0.0),
            "trigger_type": candidate.get("trigger_type"),
            "first_price": round(first_price, 4),
            "entry": round(entry, 4),
            "target": round(float(plan_target), 4) if plan_target else None,
            "stop": round(float(plan_stop), 4) if plan_stop else None,
            "planned_rr": self._rr(candidate),
            "plan_valid": plan_valid,
            "entry_triggered": entry_triggered,
            "outcome": outcome,
            "stop_before_target": stop_before_target,
            "observation_count": len(future),
            "metrics": {
                "mfe_pct": self._pct(max_price, first_price),
                "mae_pct": self._pct(min_price, first_price),
                "post_entry_mfe_pct": post_mfe,
                "post_entry_mae_pct": post_mae,
                "one_hour_return_pct": self._pct(one_hour_obs["price"], first_price) if one_hour_obs else None,
                "eod_return_pct": self._pct(eod_obs["price"], first_price) if eod_obs else None,
                "hit_plus_1_pct": plus1_obs is not None,
                "hit_plus_2_pct": plus2_obs is not None,
                "time_to_entry_minutes": time_to_entry,
                "time_to_target_minutes": time_to_target,
                "time_to_stop_minutes": time_to_stop,
            },
            "initial_context": {
                "rsi_14": self._num(candidate.get("rsi_14")),
                "rvol": self._num(candidate.get("vol_ratio")),
                "dist_to_resistance_pct": self._num(candidate.get("dist_to_resistance_pct")),
                "squeeze_days": int(self._num(candidate.get("squeeze_days"), 0.0) or 0),
                "atr_contraction_pct": self._num(candidate.get("atr_contraction_pct")),
                "pattern_bias": int(self._num(candidate.get("pattern_bias"), 0.0) or 0),
                "news_score": self._num(candidate.get("news_score")),
                "is_nr7": bool(candidate.get("is_nr7")),
            },
            "context_tags": tags,
            "possible_mistake_flags": list(dict.fromkeys(mistake_flags))[:12],
        }

    def cases(self) -> List[Dict[str, Any]]:
        rows = self._rows()
        if not rows:
            return []
        by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_symbol[row["symbol"]].append(row)
        history_end = rows[-1]["ts"]
        cases = [
            self._build_case(origin, by_symbol[origin["symbol"]], history_end)
            for origin in self._origins(rows)
        ]
        cases.sort(key=lambda c: c["first_seen"])
        return cases

    def _summarize(self, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        entered = [c for c in cases if c["entry_triggered"]]
        valid = [c for c in cases if c["plan_valid"]]
        targets = [c for c in cases if c["outcome"] == "TARGET_HIT"]
        stops = [c for c in cases if c["outcome"] == "STOPPED_OUT"]
        no_entry = [c for c in cases if c["outcome"] == "NO_ENTRY"]
        expired = [c for c in cases if c["outcome"] == "EXPIRED"]
        tracking = [c for c in cases if c["outcome"] == "TRACKING"]
        resolved_trades = len(targets) + len(stops)
        entered_with_followup = [c for c in entered if c["observation_count"] >= 2]

        return {
            "cases": len(cases),
            "valid_plans": len(valid),
            "entered": len(entered),
            "entry_rate_pct": self._rate(len(entered), len(valid)),
            "target_hits": len(targets),
            "stops": len(stops),
            "no_entry": len(no_entry),
            "expired": len(expired),
            "tracking": len(tracking),
            "target_hit_rate_pct": self._rate(len(targets), resolved_trades),
            "stop_before_target_rate_pct": self._rate(len(stops), resolved_trades),
            "plus_1_observed_rate_pct": self._rate(
                sum(1 for c in entered_with_followup if c["metrics"]["hit_plus_1_pct"]),
                len(entered_with_followup),
            ),
            "plus_2_observed_rate_pct": self._rate(
                sum(1 for c in entered_with_followup if c["metrics"]["hit_plus_2_pct"]),
                len(entered_with_followup),
            ),
            "avg_mfe_pct": self._avg(c["metrics"]["mfe_pct"] for c in cases),
            "avg_mae_pct": self._avg(c["metrics"]["mae_pct"] for c in cases),
            "avg_post_entry_mfe_pct": self._avg(c["metrics"]["post_entry_mfe_pct"] for c in entered),
            "avg_post_entry_mae_pct": self._avg(c["metrics"]["post_entry_mae_pct"] for c in entered),
            "avg_one_hour_return_pct": self._avg(c["metrics"]["one_hour_return_pct"] for c in cases),
            "avg_eod_return_pct": self._avg(c["metrics"]["eod_return_pct"] for c in cases),
        }

    def _setup_performance(self, cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for case in cases:
            groups[str(case.get("setup_type") or "unknown")].append(case)
        rows: List[Dict[str, Any]] = []
        for setup, group in groups.items():
            summary = self._summarize(group)
            rows.append({"setup_type": setup, **summary})
        rows.sort(key=lambda r: (-r["cases"], r["setup_type"]))
        return rows

    def _possible_mistakes(self, cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        counter: Counter[str] = Counter()
        affected: Dict[str, set] = defaultdict(set)
        for case in cases:
            if case["outcome"] not in {"STOPPED_OUT", "NO_ENTRY", "EXPIRED"}:
                continue
            for flag in case.get("possible_mistake_flags", []):
                counter[flag] += 1
                affected[flag].add(case["case_id"])
        return [
            {"label": label, "count": count, "cases": len(affected[label])}
            for label, count in counter.most_common(12)
        ]

    def _walk_forward(self, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        completed = [
            c for c in cases
            if c["outcome"] in {"TARGET_HIT", "STOPPED_OUT", "NO_ENTRY", "EXPIRED"}
        ]
        completed.sort(key=lambda c: c["first_seen"])
        n = len(completed)
        if n < 20:
            return {
                "stage": "COLLECTING",
                "completed_cases": n,
                "folds": [],
                "note": "Walk-forward için en az 20 tamamlanmış vaka bekleniyor.",
            }

        fold_size = max(5, n // 4)
        folds: List[Dict[str, Any]] = []
        start = fold_size
        fold_no = 1
        while start < n:
            test = completed[start : min(start + fold_size, n)]
            if not test:
                break
            train = completed[:start]
            train_summary = self._summarize(train)
            test_summary = self._summarize(test)

            setup_rows: List[Dict[str, Any]] = []
            setup_names = sorted({str(c.get("setup_type") or "unknown") for c in test})
            for setup in setup_names:
                tr = [c for c in train if str(c.get("setup_type") or "unknown") == setup]
                te = [c for c in test if str(c.get("setup_type") or "unknown") == setup]
                if len(tr) < 3 or len(te) < 2:
                    continue
                tr_s = self._summarize(tr)
                te_s = self._summarize(te)
                train_hit = tr_s.get("target_hit_rate_pct")
                test_hit = te_s.get("target_hit_rate_pct")
                setup_rows.append({
                    "setup_type": setup,
                    "train_cases": len(tr),
                    "test_cases": len(te),
                    "train_hit_rate_pct": train_hit,
                    "test_hit_rate_pct": test_hit,
                    "hit_rate_change_pp": (
                        round(float(test_hit) - float(train_hit), 1)
                        if train_hit is not None and test_hit is not None else None
                    ),
                    "train_entry_rate_pct": tr_s.get("entry_rate_pct"),
                    "test_entry_rate_pct": te_s.get("entry_rate_pct"),
                    "test_plus_1_rate_pct": te_s.get("plus_1_observed_rate_pct"),
                    "test_plus_2_rate_pct": te_s.get("plus_2_observed_rate_pct"),
                })

            train_hit = train_summary.get("target_hit_rate_pct")
            test_hit = test_summary.get("target_hit_rate_pct")
            folds.append({
                "fold": fold_no,
                "train_cases": len(train),
                "test_cases": len(test),
                "train_period_end": train[-1]["case_date"],
                "test_period_start": test[0]["case_date"],
                "test_period_end": test[-1]["case_date"],
                "train_hit_rate_pct": train_hit,
                "test_hit_rate_pct": test_hit,
                "hit_rate_change_pp": (
                    round(float(test_hit) - float(train_hit), 1)
                    if train_hit is not None and test_hit is not None else None
                ),
                "train_entry_rate_pct": train_summary.get("entry_rate_pct"),
                "test_entry_rate_pct": test_summary.get("entry_rate_pct"),
                "test_avg_mfe_pct": test_summary.get("avg_post_entry_mfe_pct"),
                "test_avg_mae_pct": test_summary.get("avg_post_entry_mae_pct"),
                "setup_performance": setup_rows,
            })
            start += fold_size
            fold_no += 1

        return {
            "stage": "ACTIVE_WALK_FORWARD" if n >= 50 else "EARLY_WALK_FORWARD",
            "completed_cases": n,
            "folds": folds,
            "note": "Her test dilimi yalnızca kendisinden önceki vakalarla karşılaştırılır; canlı ağırlıklar otomatik değiştirilmez.",
        }

    def report(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        all_cases = self.cases()
        clean = (symbol or "").upper().strip()
        selected = [c for c in all_cases if c["symbol"] == clean] if clean else all_cases

        setup_rows = self._setup_performance(all_cases)
        if clean:
            relevant_setups = {str(c.get("setup_type") or "unknown") for c in selected}
            relevant_setup_rows = [r for r in setup_rows if r["setup_type"] in relevant_setups]
        else:
            relevant_setup_rows = setup_rows

        return {
            "symbol": clean or None,
            "shadow_only": True,
            "auto_tuning_enabled": False,
            "measurement": "scan_observed",
            "summary": self._summarize(selected),
            "recent_cases": list(reversed(selected[-8:])),
            "setup_performance": relevant_setup_rows[:12],
            "possible_mistake_patterns": self._possible_mistakes(all_cases if not clean else selected),
            "walk_forward": self._walk_forward(all_cases),
            "method": {
                "name": "Outcome Case Analytics v1",
                "source": "persisted scan_history snapshots",
                "horizon_days": self.HORIZON_DAYS,
                "one_hour_window_minutes": [self.ONE_HOUR_MINUTES, self.ONE_HOUR_MAX_MINUTES],
                "intrabar_high_low_available": False,
                "future_data_used_for_live_decision": False,
                "production_weights_changed": False,
                "note": "MFE/MAE and target/stop ordering are based on observed scanner snapshot prices, not unseen intrabar extremes.",
            },
        }
