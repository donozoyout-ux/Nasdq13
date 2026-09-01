"""Guarded adaptive learning for NASDQ13 candidate ranking.

This module is intentionally conservative. It may only re-rank already valid
mid-cap candidates after enough completed historical cases exist and after the
same factor direction survives chronological walk-forward checks.

It never rewrites the underlying strategy formulas, thresholds, stops, targets,
or risk settings. The base setup score is preserved; an adaptive score is a
bounded overlay used only for ordering candidates when the guardrails are open.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.analysis.learning_case_analytics import LearningCaseAnalytics


class AdaptiveLearningEngine:
    """Walk-forward-gated ranking overlay learned from resolved scan cases."""

    COMPLETED = {"TARGET_HIT", "STOPPED_OUT", "NO_ENTRY", "EXPIRED"}
    SUCCESS = "TARGET_HIT"

    DEFAULTS = {
        "enabled": True,
        "min_completed_cases": 100,
        "min_factor_cases": 20,
        "min_factor_train_cases": 10,
        "min_factor_test_cases": 3,
        "min_valid_folds": 2,
        "fold_count": 3,
        "min_abs_walk_forward_lift_pp": 5.0,
        "max_factor_adjustment_points": 2.0,
        "max_total_adjustment_points": 5.0,
        "min_score": 0.0,
        "max_score": 100.0,
    }

    def __init__(self, state: Optional[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> None:
        self.state = state or {}
        raw = ((config or {}).get("smallcap", {}) or {}).get("adaptive_learning", {}) or {}
        self.cfg = {**self.DEFAULTS, **raw}
        self.enabled = bool(self.cfg.get("enabled", True))
        self._last_status: Dict[str, Any] = {}

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
    def _clip(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    @staticmethod
    def _bucket(value: float, edges: Sequence[Tuple[float, str]], fallback: str) -> str:
        for upper, label in edges:
            if value < upper:
                return label
        return fallback

    @classmethod
    def _feature_tokens(cls, row: Dict[str, Any]) -> Dict[str, str]:
        """Only use features available before the intraday trigger/news enrichment."""
        tokens: Dict[str, str] = {}

        setup = str(row.get("setup_type") or "unknown").lower()
        tokens[f"setup:{setup}"] = f"Setup: {setup}"

        setup_score = cls._num(row.get("setup_score"))
        setup_bucket = cls._bucket(
            setup_score,
            [(55, "<55"), (65, "55-64"), (75, "65-74"), (85, "75-84")],
            "85+",
        )
        tokens[f"setup_score:{setup_bucket}"] = f"Setup skoru {setup_bucket}"

        anticipation = cls._num(row.get("anticipation_score"))
        anticipation_bucket = cls._bucket(
            anticipation,
            [(50, "<50"), (65, "50-64"), (80, "65-79")],
            "80+",
        )
        tokens[f"anticipation:{anticipation_bucket}"] = f"Öngörü skoru {anticipation_bucket}"

        rsi = cls._num(row.get("rsi_14"), 50.0)
        rsi_bucket = cls._bucket(
            rsi,
            [(42, "<42"), (52, "42-51"), (70, "52-69"), (76, "70-75")],
            "76+",
        )
        tokens[f"rsi:{rsi_bucket}"] = f"RSI {rsi_bucket}"

        rvol = cls._num(row.get("vol_ratio"), 1.0)
        rvol_bucket = cls._bucket(
            rvol,
            [(0.8, "<0.8x"), (1.0, "0.8-0.99x"), (1.2, "1.0-1.19x"), (1.5, "1.2-1.49x")],
            "1.5x+",
        )
        tokens[f"rvol:{rvol_bucket}"] = f"RVOL {rvol_bucket}"

        dist = cls._num(row.get("dist_to_resistance_pct"), 99.0)
        dist_bucket = cls._bucket(
            dist,
            [(0, "above"), (1.5, "0-1.49%"), (4, "1.5-3.99%"), (8, "4-7.99%")],
            "8%+",
        )
        tokens[f"distance_resistance:{dist_bucket}"] = f"Dirence mesafe {dist_bucket}"

        squeeze = int(cls._num(row.get("squeeze_days"), 0.0))
        squeeze_bucket = "0-2" if squeeze < 3 else "3-7" if squeeze < 8 else "8+"
        tokens[f"squeeze_days:{squeeze_bucket}"] = f"Squeeze {squeeze_bucket} gün"

        atr_contract = cls._num(row.get("atr_contraction_pct"), 0.0)
        atr_state = (
            "strong_contraction" if atr_contract <= -12
            else "contraction" if atr_contract <= -4
            else "flat" if atr_contract < 8
            else "expansion"
        )
        tokens[f"atr_state:{atr_state}"] = f"ATR {atr_state}"

        pattern_bias = int(cls._num(row.get("pattern_bias"), 0.0))
        bias = "bullish" if pattern_bias > 0 else "bearish" if pattern_bias < 0 else "neutral"
        tokens[f"pattern_bias:{bias}"] = f"Mum bias {bias}"

        nr7 = "yes" if bool(row.get("is_nr7")) else "no"
        tokens[f"nr7:{nr7}"] = f"NR7 {nr7}"
        return tokens

    @classmethod
    def _case_row(cls, case: Dict[str, Any]) -> Dict[str, Any]:
        context = case.get("initial_context") or {}
        return {
            "setup_type": case.get("setup_type"),
            "setup_score": case.get("setup_score"),
            "anticipation_score": case.get("anticipation_score"),
            "rsi_14": context.get("rsi_14"),
            "vol_ratio": context.get("rvol"),
            "dist_to_resistance_pct": context.get("dist_to_resistance_pct"),
            "squeeze_days": context.get("squeeze_days"),
            "atr_contraction_pct": context.get("atr_contraction_pct"),
            "pattern_bias": context.get("pattern_bias"),
            "is_nr7": context.get("is_nr7"),
        }

    def _completed_cases(self) -> List[Dict[str, Any]]:
        cases = LearningCaseAnalytics(self.state).cases()
        completed = [c for c in cases if c.get("outcome") in self.COMPLETED]
        completed.sort(key=lambda c: str(c.get("first_seen") or ""))
        return completed

    @staticmethod
    def _success_rate(cases: Iterable[Dict[str, Any]]) -> Optional[float]:
        rows = list(cases)
        if not rows:
            return None
        return sum(1 for c in rows if c.get("outcome") == AdaptiveLearningEngine.SUCCESS) / len(rows) * 100.0

    def _folds(self, cases: List[Dict[str, Any]]) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
        n = len(cases)
        count = max(1, int(self.cfg.get("fold_count", 3)))
        if n < int(self.cfg.get("min_completed_cases", 100)):
            return []

        initial = max(40, int(n * 0.40))
        remaining = n - initial
        if remaining < count * 5:
            return []
        test_size = max(5, remaining // count)
        folds: List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = []
        start = initial
        while start < n and len(folds) < count:
            end = n if len(folds) == count - 1 else min(n, start + test_size)
            train = cases[:start]
            test = cases[start:end]
            if test:
                folds.append((train, test))
            start = end
        return folds

    def _factor_model(self, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = len(cases)
        baseline = self._success_rate(cases)
        folds = self._folds(cases)
        min_total = int(self.cfg.get("min_completed_cases", 100))
        min_factor = int(self.cfg.get("min_factor_cases", 20))
        min_train = int(self.cfg.get("min_factor_train_cases", 10))
        min_test = int(self.cfg.get("min_factor_test_cases", 3))
        min_valid_folds = int(self.cfg.get("min_valid_folds", 2))
        min_lift = float(self.cfg.get("min_abs_walk_forward_lift_pp", 5.0))
        max_factor_adjust = float(self.cfg.get("max_factor_adjustment_points", 2.0))

        counts: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "wins": 0, "label": ""})
        for case in cases:
            for token, label in self._feature_tokens(self._case_row(case)).items():
                row = counts[token]
                row["n"] += 1
                row["wins"] += int(case.get("outcome") == self.SUCCESS)
                row["label"] = label

        approved: Dict[str, Dict[str, Any]] = {}
        rejected_reason = ""
        if not self.enabled:
            rejected_reason = "disabled"
        elif total < min_total:
            rejected_reason = "not_enough_completed_cases"
        elif len(folds) < min_valid_folds:
            rejected_reason = "not_enough_walk_forward_folds"

        if not rejected_reason:
            for token, row in counts.items():
                if row["n"] < min_factor:
                    continue
                fold_lifts: List[float] = []
                fold_details: List[Dict[str, Any]] = []
                for idx, (train, test) in enumerate(folds, start=1):
                    train_factor = [c for c in train if token in self._feature_tokens(self._case_row(c))]
                    test_factor = [c for c in test if token in self._feature_tokens(self._case_row(c))]
                    if len(train_factor) < min_train or len(test_factor) < min_test:
                        continue
                    test_base = self._success_rate(test)
                    test_factor_rate = self._success_rate(test_factor)
                    if test_base is None or test_factor_rate is None:
                        continue
                    lift = test_factor_rate - test_base
                    fold_lifts.append(lift)
                    fold_details.append({
                        "fold": idx,
                        "test_samples": len(test_factor),
                        "test_hit_rate_pct": round(test_factor_rate, 1),
                        "test_baseline_pct": round(test_base, 1),
                        "lift_pp": round(lift, 1),
                    })

                if len(fold_lifts) < min_valid_folds:
                    continue
                avg_lift = mean(fold_lifts)
                med_lift = median(fold_lifts)
                latest_lift = fold_lifts[-1]
                direction = 1 if avg_lift > 0 else -1 if avg_lift < 0 else 0
                same_direction = sum(1 for x in fold_lifts if (x > 0) == (direction > 0)) if direction else 0
                consistency = same_direction / len(fold_lifts) if fold_lifts else 0.0

                # Require the median and the latest out-of-sample fold to agree.
                stable = (
                    direction != 0
                    and abs(avg_lift) >= min_lift
                    and consistency >= 2 / 3
                    and (med_lift > 0) == (direction > 0)
                    and (latest_lift > 0) == (direction > 0)
                )
                if not stable:
                    continue

                confidence = min(1.0, row["n"] / 60.0) * min(1.0, len(fold_lifts) / max(1, len(folds)))
                raw_adjust = avg_lift / 10.0 * confidence
                adjust = self._clip(raw_adjust, -max_factor_adjust, max_factor_adjust)
                if abs(adjust) < 0.25:
                    continue

                approved[token] = {
                    "token": token,
                    "label": row["label"],
                    "samples": row["n"],
                    "historical_hit_rate_pct": round(row["wins"] / row["n"] * 100.0, 1),
                    "walk_forward_lift_pp": round(avg_lift, 1),
                    "walk_forward_median_lift_pp": round(med_lift, 1),
                    "consistency_pct": round(consistency * 100.0, 1),
                    "adjustment_points": round(adjust, 2),
                    "folds": fold_details,
                }

        stage = "DISABLED" if not self.enabled else "COLLECTING" if total < min_total else "ACTIVE" if approved else "VALIDATING"
        return {
            "enabled": self.enabled,
            "stage": stage,
            "completed_cases": total,
            "baseline_hit_rate_pct": round(baseline, 1) if baseline is not None else None,
            "walk_forward_folds": len(folds),
            "approved_factors": approved,
            "approved_factor_count": len(approved),
            "blocked_reason": rejected_reason or None,
            "guardrails": {
                "min_completed_cases": min_total,
                "min_factor_cases": min_factor,
                "min_valid_folds": min_valid_folds,
                "min_abs_walk_forward_lift_pp": min_lift,
                "max_factor_adjustment_points": max_factor_adjust,
                "max_total_adjustment_points": float(self.cfg.get("max_total_adjustment_points", 5.0)),
                "base_strategy_thresholds_mutated": False,
                "risk_parameters_mutated": False,
            },
        }

    def rank_candidates(self, candidates: Sequence[Any]) -> Tuple[List[Any], Dict[str, Any]]:
        """Attach adaptive metadata and re-rank only when the safety gate is ACTIVE."""
        cases = self._completed_cases()
        model = self._factor_model(cases)
        approved = model.get("approved_factors", {}) or {}
        max_total = float(self.cfg.get("max_total_adjustment_points", 5.0))
        score_low = float(self.cfg.get("min_score", 0.0))
        score_high = float(self.cfg.get("max_score", 100.0))
        active = model.get("stage") == "ACTIVE"

        ranked: List[Any] = []
        candidate_rows: List[Dict[str, Any]] = []
        for candidate in candidates:
            row = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate)
            base = self._num(row.get("setup_score"))
            tokens = self._feature_tokens(row)
            matches = [approved[t] for t in tokens if t in approved]
            adjustment = sum(self._num(m.get("adjustment_points")) for m in matches) if active else 0.0
            adjustment = self._clip(adjustment, -max_total, max_total)
            adaptive_score = self._clip(base + adjustment, score_low, score_high)

            # Dataclasses here are not slotted, so runtime metadata is safe and
            # leaves the canonical setup_score untouched for auditability.
            try:
                setattr(candidate, "adaptive_base_score", round(base, 2))
                setattr(candidate, "adaptive_adjustment", round(adjustment, 2))
                setattr(candidate, "adaptive_score", round(adaptive_score, 2))
                setattr(candidate, "adaptive_learning_applied", bool(active and matches and adjustment != 0))
                setattr(candidate, "adaptive_factors", [
                    {
                        "label": m.get("label"),
                        "adjustment_points": m.get("adjustment_points"),
                        "walk_forward_lift_pp": m.get("walk_forward_lift_pp"),
                        "samples": m.get("samples"),
                    }
                    for m in matches[:6]
                ])
            except Exception:
                pass

            candidate_rows.append({
                "symbol": row.get("symbol"),
                "base_score": round(base, 2),
                "adaptive_score": round(adaptive_score, 2),
                "adjustment": round(adjustment, 2),
                "matched_factors": len(matches),
            })
            ranked.append(candidate)

        ranked.sort(
            key=lambda c: (
                self._num(getattr(c, "adaptive_score", getattr(c, "setup_score", 0.0))),
                self._num(getattr(c, "setup_score", 0.0)),
                self._num(getattr(c, "anticipation_score", 0.0)),
            ),
            reverse=True,
        )

        status = {
            **model,
            "live_reranking_active": active,
            "candidate_count": len(ranked),
            "top_candidates": sorted(candidate_rows, key=lambda r: (r["adaptive_score"], r["base_score"]), reverse=True)[:10],
            "note": (
                "Adaptive overlay aktif: yalnız walk-forward onaylı faktörler aday sırasını en fazla ±5 puan etkileyebilir."
                if active
                else "Adaptive overlay henüz aday sırasını değiştirmiyor; veri/validasyon kapısı bekleniyor."
            ),
        }
        self._last_status = status
        return ranked, status

    def status(self) -> Dict[str, Any]:
        if self._last_status:
            return self._last_status
        return self._factor_model(self._completed_cases())
