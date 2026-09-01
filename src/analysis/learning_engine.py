"""Outcome-driven learning journal for NASDQ13.

This module learns descriptive lessons from *resolved live prediction records*
and their original small-cap scan snapshots. It is intentionally shadow-only:
it never changes production ranking weights, thresholds, orders, or alerts.

The goal is to turn mistakes into evidence:
- join each resolved prediction (hit / stop / expired) to its first scan context,
- bucket the original features,
- measure which contexts over/under-perform the baseline,
- expose positive/negative lessons and a bounded shadow score adjustment,
- require minimum sample counts before any factor is treated as actionable.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


class LearningEngine:
    """Derive evidence-backed lessons from persisted bot state."""

    MIN_LESSON_SAMPLES = 5
    MIN_FACTOR_ADJUST_SAMPLES = 8
    MIN_SHADOW_TOTAL = 20
    PRIOR_STRENGTH = 6.0
    MAX_SHADOW_DELTA = 12.0

    def __init__(self, state: Optional[Dict[str, Any]]) -> None:
        self.state = state or {}

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
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
    def _bucket(value: float, edges: Iterable[Tuple[float, str]], fallback: str) -> str:
        for upper, label in edges:
            if value < upper:
                return label
        return fallback

    @classmethod
    def _feature_tokens(cls, candidate: Dict[str, Any]) -> Dict[str, str]:
        """Convert a raw candidate snapshot into stable categorical features."""
        tokens: Dict[str, str] = {}

        setup_type = str(candidate.get("setup_type") or "unknown").upper()
        tokens[f"setup_type:{setup_type}"] = f"Setup tipi: {setup_type}"

        setup_score = cls._num(candidate.get("setup_score"))
        setup_bucket = cls._bucket(
            setup_score,
            [(55, "<55"), (65, "55-64"), (75, "65-74"), (85, "75-84")],
            "85+",
        )
        tokens[f"setup_score:{setup_bucket}"] = f"Setup skoru: {setup_bucket}"

        anticipation = cls._num(candidate.get("anticipation_score"))
        anticipation_bucket = cls._bucket(
            anticipation,
            [(50, "<50"), (65, "50-64"), (80, "65-79")],
            "80+",
        )
        tokens[f"anticipation:{anticipation_bucket}"] = f"Öngörü skoru: {anticipation_bucket}"

        rsi = cls._num(candidate.get("rsi_14"), 50.0)
        rsi_bucket = cls._bucket(
            rsi,
            [(42, "<42"), (52, "42-51"), (70, "52-69"), (76, "70-75")],
            "76+",
        )
        tokens[f"rsi:{rsi_bucket}"] = f"RSI: {rsi_bucket}"

        rvol = cls._num(candidate.get("vol_ratio"), 1.0)
        rvol_bucket = cls._bucket(
            rvol,
            [(0.7, "<0.7x"), (1.0, "0.7-0.99x"), (1.2, "1.0-1.19x"), (1.5, "1.2-1.49x")],
            "1.5x+",
        )
        tokens[f"rvol:{rvol_bucket}"] = f"RVOL: {rvol_bucket}"

        dist_res = cls._num(candidate.get("dist_to_resistance_pct"))
        dist_bucket = cls._bucket(
            dist_res,
            [(0, "above"), (1.5, "0-1.49%"), (4, "1.5-3.99%"), (8, "4-7.99%")],
            "8%+",
        )
        tokens[f"distance_resistance:{dist_bucket}"] = f"Dirence mesafe: {dist_bucket}"

        squeeze_days = int(cls._num(candidate.get("squeeze_days")))
        squeeze_bucket = "0-2" if squeeze_days < 3 else "3-7" if squeeze_days < 8 else "8+"
        tokens[f"squeeze_days:{squeeze_bucket}"] = f"Sıkışma süresi: {squeeze_bucket} gün"

        atr_contract = cls._num(candidate.get("atr_contraction_pct"))
        atr_bucket = (
            "strong_contraction" if atr_contract <= -12
            else "contraction" if atr_contract <= -4
            else "flat" if atr_contract < 8
            else "expansion"
        )
        tokens[f"atr_state:{atr_bucket}"] = f"ATR durumu: {atr_bucket}"

        pattern_bias = int(cls._num(candidate.get("pattern_bias")))
        bias_label = "bullish" if pattern_bias > 0 else "bearish" if pattern_bias < 0 else "neutral"
        tokens[f"pattern_bias:{bias_label}"] = f"Mum bias: {bias_label}"

        trigger = str(candidate.get("trigger_type") or "none").lower()
        tokens[f"trigger:{trigger}"] = f"15M tetik: {trigger}"

        nr7 = "yes" if bool(candidate.get("is_nr7")) else "no"
        tokens[f"nr7:{nr7}"] = f"NR7: {nr7}"

        news = cls._num(candidate.get("news_score"))
        news_label = "positive" if news >= 5 else "negative" if news <= -5 else "neutral"
        tokens[f"news:{news_label}"] = f"Haber sentiment: {news_label}"

        for pattern in (candidate.get("candle_patterns") or [])[:5]:
            name = str(pattern).strip().upper()
            if name:
                tokens[f"candle:{name}"] = f"Mum formasyonu: {name}"

        trade_plan = candidate.get("trade_plan") or {}
        entry = cls._num(trade_plan.get("limit"))
        target = cls._num(trade_plan.get("target"))
        stop = cls._num(trade_plan.get("stop"))
        if entry > 0 and target > entry and 0 < stop < entry:
            rr = (target - entry) / max(entry - stop, 1e-9)
            rr_bucket = cls._bucket(rr, [(1.5, "<1.5R"), (2.0, "1.5-1.99R"), (3.0, "2.0-2.99R")], "3R+")
            tokens[f"planned_rr:{rr_bucket}"] = f"Planlanan R:R: {rr_bucket}"

        return tokens

    def _scan_rows(self) -> List[Tuple[datetime, Dict[str, Any]]]:
        rows: List[Tuple[datetime, Dict[str, Any]]] = []
        for scan in self.state.get("scan_history", []) or []:
            ts = self._parse_ts(scan.get("time"))
            if ts is None:
                continue
            for candidate in scan.get("candidates", []) or []:
                if isinstance(candidate, dict) and candidate.get("symbol"):
                    rows.append((ts, candidate))
        return rows

    def _resolved_predictions(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        track = self.state.get("prediction_tracker", {}) or {}
        for rec in track.values():
            if not isinstance(rec, dict):
                continue
            # Learn the daily scanner first. Weekly/Telegram have different entry semantics.
            if rec.get("source", "daily") != "daily":
                continue
            outcome = rec.get("outcome")
            status = rec.get("status")
            if outcome in {"hit", "stop", "expired"} or status in {"target_hit", "stopped_out", "expired"}:
                records.append(rec)
        return records

    def _match_candidate(
        self,
        rec: Dict[str, Any],
        scan_rows: List[Tuple[datetime, Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        symbol = str(rec.get("symbol") or "").upper()
        first_seen = self._parse_ts(rec.get("first_seen"))
        if not symbol:
            return None

        matches: List[Tuple[float, Dict[str, Any]]] = []
        for ts, candidate in scan_rows:
            if str(candidate.get("symbol") or "").upper() != symbol:
                continue
            if first_seen is None:
                matches.append((0.0, candidate))
                continue
            distance_hours = abs((ts - first_seen).total_seconds()) / 3600.0
            if distance_hours <= 48:
                matches.append((distance_hours, candidate))
        if not matches:
            return None
        matches.sort(key=lambda item: item[0])
        return matches[0][1]

    @staticmethod
    def _normalize_outcome(rec: Dict[str, Any]) -> str:
        outcome = rec.get("outcome")
        status = rec.get("status")
        if outcome == "hit" or status == "target_hit":
            return "hit"
        if outcome == "stop" or status == "stopped_out":
            return "stop"
        return "expired"

    def _samples(self) -> List[Dict[str, Any]]:
        scan_rows = self._scan_rows()
        samples: List[Dict[str, Any]] = []
        for rec in self._resolved_predictions():
            candidate = self._match_candidate(rec, scan_rows)
            if candidate is None:
                continue
            first_price = self._num(rec.get("first_price"))
            last_price = self._num(rec.get("last_price"))
            return_pct = ((last_price / first_price) - 1) * 100 if first_price > 0 and last_price > 0 else None
            samples.append({
                "symbol": rec.get("symbol"),
                "first_seen": rec.get("first_seen"),
                "resolved_at": rec.get("resolved_at"),
                "outcome": self._normalize_outcome(rec),
                "return_pct": round(return_pct, 3) if return_pct is not None else None,
                "candidate": candidate,
                "tokens": self._feature_tokens(candidate),
            })
        return samples

    @staticmethod
    def _stage(resolved: int) -> str:
        if resolved < 20:
            return "COLLECTING"
        if resolved < 100:
            return "SHADOW_LEARNING"
        return "VALIDATION_READY"

    def report(self) -> Dict[str, Any]:
        samples = self._samples()
        total = len(samples)
        wins = sum(1 for s in samples if s["outcome"] == "hit")
        stops = sum(1 for s in samples if s["outcome"] == "stop")
        expired = sum(1 for s in samples if s["outcome"] == "expired")
        baseline = (wins / total) if total else 0.0

        stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "n": 0, "wins": 0, "stops": 0, "expired": 0, "returns": [], "label": ""
        })

        for sample in samples:
            for token, label in sample["tokens"].items():
                row = stats[token]
                row["n"] += 1
                row["label"] = label
                if sample["outcome"] == "hit":
                    row["wins"] += 1
                elif sample["outcome"] == "stop":
                    row["stops"] += 1
                else:
                    row["expired"] += 1
                if sample["return_pct"] is not None:
                    row["returns"].append(sample["return_pct"])

        factor_rows: List[Dict[str, Any]] = []
        for token, row in stats.items():
            n = row["n"]
            raw_rate = row["wins"] / n if n else 0.0
            smoothed = (
                row["wins"] + baseline * self.PRIOR_STRENGTH
            ) / (n + self.PRIOR_STRENGTH) if n else baseline
            lift_pp = (smoothed - baseline) * 100.0
            avg_return = sum(row["returns"]) / len(row["returns"]) if row["returns"] else None
            factor_rows.append({
                "token": token,
                "label": row["label"],
                "samples": n,
                "wins": row["wins"],
                "stops": row["stops"],
                "expired": row["expired"],
                "raw_hit_rate_pct": round(raw_rate * 100.0, 1),
                "smoothed_hit_rate_pct": round(smoothed * 100.0, 1),
                "lift_pp": round(lift_pp, 1),
                "avg_return_pct": round(avg_return, 3) if avg_return is not None else None,
                "confidence_pct": round(min(100.0, n / 30.0 * 100.0), 1),
                "actionable": n >= self.MIN_FACTOR_ADJUST_SAMPLES,
            })

        qualified = [f for f in factor_rows if f["samples"] >= self.MIN_LESSON_SAMPLES]
        positive = sorted(
            [f for f in qualified if f["lift_pp"] >= 3.0],
            key=lambda f: (f["lift_pp"], f["samples"]),
            reverse=True,
        )[:10]
        negative = sorted(
            [f for f in qualified if f["lift_pp"] <= -3.0],
            key=lambda f: (f["lift_pp"], -f["samples"]),
        )[:10]

        mistakes = []
        for sample in reversed(samples):
            if sample["outcome"] not in {"stop", "expired"}:
                continue
            mistakes.append({
                "symbol": sample["symbol"],
                "outcome": sample["outcome"],
                "first_seen": sample["first_seen"],
                "resolved_at": sample["resolved_at"],
                "return_pct": sample["return_pct"],
                "context": list(sample["tokens"].values())[:8],
            })
            if len(mistakes) >= 12:
                break

        return {
            "stage": self._stage(total),
            "shadow_only": True,
            "production_weights_changed": False,
            "minimums": {
                "lesson_samples": self.MIN_LESSON_SAMPLES,
                "factor_adjust_samples": self.MIN_FACTOR_ADJUST_SAMPLES,
                "shadow_total_samples": self.MIN_SHADOW_TOTAL,
            },
            "summary": {
                "resolved_samples": total,
                "wins": wins,
                "stops": stops,
                "expired": expired,
                "baseline_hit_rate_pct": round(baseline * 100.0, 1) if total else None,
                "matched_factor_count": len(factor_rows),
            },
            "positive_lessons": positive,
            "negative_lessons": negative,
            "recent_mistakes": mistakes,
            "all_factors": sorted(factor_rows, key=lambda f: (-f["samples"], f["token"])),
            "method": {
                "name": "Outcome Learning Journal v1",
                "source": "resolved daily prediction_tracker + original scan_history snapshot",
                "uses_future_data_for_live_decision": False,
                "auto_tuning_enabled": False,
                "note": "Lessons are descriptive/shadow until enough independent resolved samples exist.",
            },
        }

    def symbol_report(self, symbol: str, candidate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        base_report = self.report()
        clean = str(symbol or "").upper().strip()
        tokens = self._feature_tokens(candidate or {}) if candidate else {}
        factor_index = {f["token"]: f for f in base_report["all_factors"]}
        matched = [factor_index[t] for t in tokens if t in factor_index]

        actionable = [
            f for f in matched
            if f["samples"] >= self.MIN_FACTOR_ADJUST_SAMPLES
        ]
        total_resolved = int(base_report["summary"]["resolved_samples"] or 0)
        ready = total_resolved >= self.MIN_SHADOW_TOTAL and bool(actionable)

        # Convert evidence lift into a conservative shadow-only adjustment.
        # Averaging avoids stacking many correlated buckets as if independent.
        if ready:
            contributions = [max(-8.0, min(8.0, f["lift_pp"] * 0.22)) for f in actionable]
            delta = sum(contributions) / max(len(contributions), 1)
            delta = max(-self.MAX_SHADOW_DELTA, min(self.MAX_SHADOW_DELTA, delta))
        else:
            delta = 0.0

        base_score = self._num((candidate or {}).get("setup_score")) if candidate else None
        shadow_score = None
        if base_score is not None:
            shadow_score = max(0.0, min(100.0, base_score + delta))

        return {
            "symbol": clean,
            "candidate_found": bool(candidate),
            "stage": base_report["stage"],
            "shadow_only": True,
            "production_weights_changed": False,
            "summary": base_report["summary"],
            "base_setup_score": round(base_score, 1) if base_score is not None else None,
            "shadow_adjustment": round(delta, 2),
            "shadow_score": round(shadow_score, 1) if shadow_score is not None else None,
            "adjustment_ready": ready,
            "matched_positive": sorted(
                [f for f in matched if f["lift_pp"] >= 3.0],
                key=lambda f: f["lift_pp"], reverse=True,
            )[:6],
            "matched_negative": sorted(
                [f for f in matched if f["lift_pp"] <= -3.0],
                key=lambda f: f["lift_pp"],
            )[:6],
            "global_positive_lessons": base_report["positive_lessons"][:5],
            "global_negative_lessons": base_report["negative_lessons"][:5],
            "recent_mistakes": base_report["recent_mistakes"][:6],
            "guardrail": (
                "Bu katman yalnızca gölge öğrenme yapar; yeterli örnek olsa bile canlı strateji ağırlıklarını otomatik değiştirmez."
            ),
        }
