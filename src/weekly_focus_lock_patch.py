"""Runtime hard-lock for Weekly Focus 20 membership.

The first Focus 20 selected in an ISO week becomes the immutable membership for
that week. Scores, status, news, chart analysis and ranking may refresh, but the
symbols themselves may not be replaced or dropped until the ISO week changes.

This module also migrates an already-created current-week record: whatever
symbols are in that record when this patch first runs become this week's locked
membership. That means a list selected earlier today is preserved after deploy.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Dict, List

from src.analysis.weekly_focus import WeeklyFocusEngine


if not getattr(WeeklyFocusEngine, "_full_week_membership_lock_patch", False):
    _base_create_week = WeeklyFocusEngine.create_week
    _base_update_record_from_live = WeeklyFocusEngine.update_record_from_live
    _base_dashboard_payload = WeeklyFocusEngine.dashboard_payload

    def _week_end_iso(engine: WeeklyFocusEngine) -> str:
        now = datetime.now(timezone.utc)
        iso = now.isocalendar()
        sunday = datetime.fromisocalendar(iso.year, iso.week, 7).date()
        return datetime.combine(sunday, time(23, 59, 59), tzinfo=timezone.utc).isoformat()

    def _symbol_list(rec: Dict[str, Any]) -> List[str]:
        raw = rec.get("locked_symbols") or rec.get("symbols") or [
            x.get("symbol") for x in (rec.get("items") or []) if isinstance(x, dict)
        ]
        seen = set()
        result: List[str] = []
        for value in raw or []:
            symbol = str(value or "").upper().strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                result.append(symbol)
        return result

    def _enforce(engine: WeeklyFocusEngine, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Restore exact weekly membership while preserving latest live fields."""
        locked = _symbol_list(rec)[: engine.size]
        if not locked:
            return rec

        # First run/migration: freeze the exact current-week list and its original
        # snapshots. Future refreshes can change values, never membership.
        if not rec.get("locked_symbols"):
            rec["locked_symbols"] = list(locked)
            rec["locked_at"] = rec.get("selected_at") or datetime.now(timezone.utc).isoformat()
            rec["locked_until"] = _week_end_iso(engine)
            rec["lock_policy"] = "symbols_fixed_until_next_iso_week"
            original_by_symbol = {
                str(x.get("symbol") or "").upper(): dict(x)
                for x in (rec.get("items") or [])
                if isinstance(x, dict) and x.get("symbol")
            }
            rec["locked_items"] = [
                original_by_symbol[s] for s in locked if s in original_by_symbol
            ]

        locked = [str(s).upper() for s in rec.get("locked_symbols", locked)][: engine.size]
        live_by_symbol = {
            str(x.get("symbol") or "").upper(): dict(x)
            for x in (rec.get("items") or [])
            if isinstance(x, dict) and x.get("symbol")
        }
        frozen_by_symbol = {
            str(x.get("symbol") or "").upper(): dict(x)
            for x in (rec.get("locked_items") or [])
            if isinstance(x, dict) and x.get("symbol")
        }

        restored: List[Dict[str, Any]] = []
        for original_rank, symbol in enumerate(locked, start=1):
            row = live_by_symbol.get(symbol) or frozen_by_symbol.get(symbol)
            if row is None:
                # Membership is still retained even if a provider temporarily
                # cannot return data for the symbol.
                row = {
                    "symbol": symbol,
                    "name": symbol,
                    "weekly_rank": original_rank,
                    "weekly_status": "WATCH",
                    "data_stale": True,
                }
            else:
                row = dict(row)
                row["data_stale"] = symbol not in live_by_symbol
            row.setdefault("weekly_rank", original_rank)
            restored.append(row)

        rec["items"] = restored
        rec["symbols"] = list(locked)
        rec["size"] = len(locked)
        rec["locked_for_week"] = True
        rec["membership_frozen"] = True
        rec.setdefault("locked_until", _week_end_iso(engine))
        rec.setdefault("lock_policy", "symbols_fixed_until_next_iso_week")
        engine.state["weekly_focus"] = rec
        return rec

    def current_record(self):
        # Read the raw state directly instead of relying on the original helper,
        # because the original considers an empty live-items array as "no week".
        # We must still recover the locked membership when all providers fail in
        # one refresh cycle.
        rec = self.state.get("weekly_focus")
        if not isinstance(rec, dict):
            return None
        if rec.get("week_key") != self.week_key():
            return None
        if not _symbol_list(rec):
            return None
        return _enforce(self, rec)

    def create_week(self, candidates, universe):
        selected, rec = _base_create_week(self, candidates, universe)
        rec = _enforce(self, rec)
        return selected, rec

    def update_record_from_live(self, candidates):
        rec = _base_update_record_from_live(self, candidates)
        if not rec:
            # If a temporary provider failure caused an empty live set, recover
            # directly from the immutable weekly snapshot instead of reselecting.
            rec = self.current_record()
        if not rec:
            return None
        return _enforce(self, rec)

    def dashboard_payload(self, *args, **kwargs):
        payload = _base_dashboard_payload(self, *args, **kwargs)
        rec = self.current_record()
        if rec:
            payload["membership_frozen"] = True
            payload["locked_symbols"] = list(rec.get("locked_symbols") or rec.get("symbols") or [])
            payload["locked_at"] = rec.get("locked_at")
            payload["locked_until"] = rec.get("locked_until")
            payload["lock_policy"] = rec.get("lock_policy")
            payload["note"] = (
                "Bu haftanın hisse üyeliği kilitlidir: hafta içinde yalnızca analiz, "
                "durum ve sıralama güncellenir; hisseler gelecek ISO haftasına kadar değişmez."
            )
        return payload

    WeeklyFocusEngine.current_record = current_record
    WeeklyFocusEngine.create_week = create_week
    WeeklyFocusEngine.update_record_from_live = update_record_from_live
    WeeklyFocusEngine.dashboard_payload = dashboard_payload
    WeeklyFocusEngine._full_week_membership_lock_patch = True
