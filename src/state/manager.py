"""
State Manager
- Persists bot state (sent signals, cooldowns, performance) to JSON
- Prevents duplicate signals across restarts
"""
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import asdict

from src.utils.logger import get_logger

logger = get_logger(__name__)


class StateManager:
    """Persists bot state to a JSON file"""

    def __init__(self, file_path: str = "data/bot_state.json", config: Optional[Dict[str, Any]] = None):
        self.file_path = file_path
        self.config = config or {}
        self.state: Dict[str, Any] = {
            "signals_sent": {},
            "signal_history": [],
            "reports_sent": {},       # report dedup: key (iso-week/date) -> timestamp
            "smallcap_alerts_sent": {},  # small-cap breakout alarm dedup: symbol-date -> timestamp
            "smallcap_setup_sent": {},   # daily setup report dedup: date -> timestamp
            "smallcap_predictions_sent": {},  # daily "tomorrow predictions" report dedup: date -> timestamp
            "smallcap_premarket_sent": {},  # daily "BUGÜN İZLE" pre-market report dedup: date -> timestamp
            "weekly_reports": [],     # last generated weekly report summaries
            "scan_history": [],       # persisted mid-cap scan snapshots (pruned)
            "last_scan_time": None,
            "stats": {
                "total_signals": 0,
                "total_scans": 0,
                "last_signal": None,
            },
            "created_at": datetime.utcnow().isoformat(),
        }
        if "reports_sent" not in self.state:
            self.state["reports_sent"] = {}
        if "weekly_reports" not in self.state:
            self.state["weekly_reports"] = []
        if "scan_history" not in self.state:
            self.state["scan_history"] = []
        self.max_history = self.config.get("state", {}).get("max_signal_history", 1000)
        self.max_scan_history = self.config.get("state", {}).get("max_scan_history", 200)
        self._load()

    def _ensure_dir(self):
        """Ensure the directory for the state file exists"""
        dirname = os.path.dirname(self.file_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

    def _load(self):
        """Load state from disk if it exists"""
        self._ensure_dir()
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.state.update(loaded)
                logger.info(f"State loaded from {self.file_path}")
            except Exception as e:
                logger.error(f"Failed to load state: {e}")

    def save(self):
        """Persist state to disk"""
        self._ensure_dir()
        try:
            # Prune history to keep file small
            if len(self.state["signal_history"]) > self.max_history:
                self.state["signal_history"] = self.state["signal_history"][-self.max_history:]

            scan_hist = self.state.get("scan_history", [])
            if len(scan_hist) > self.max_scan_history:
                self.state["scan_history"] = scan_hist[-self.max_scan_history:]

            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False, default=str)
            logger.debug(f"State saved to {self.file_path}")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def load_raw(self, raw: Dict[str, Any]) -> None:
        """Replace in-memory state from a raw dict (e.g. restored from a GitHub
        backup). Missing keys are re-seeded so new fields never break old files."""
        if not isinstance(raw, dict):
            logger.warning("State restore skipped: raw data is not a dict")
            return
        for key, default in self.state.items():
            if key in raw:
                self.state[key] = raw[key]
            else:
                self.state[key] = default
        logger.info(f"State restored from raw backup ({len(raw)} top-level keys)")
        self.save()

    def is_signal_duplicate(self, signal_id: str) -> bool:
        """Check if a signal was already sent (dedup across restarts)"""
        return signal_id in self.state["signals_sent"]

    def record_signal(self, signal) -> None:
        """Record a sent signal"""
        sig_id = signal.signal_id
        self.state["signals_sent"][sig_id] = datetime.utcnow().isoformat()

        self.state["signal_history"].append({
            "id": sig_id,
            "symbol": signal.symbol,
            "action": signal.action,
            "direction": signal.direction,
            "strength": signal.strength,
            "price": signal.price,
            "timestamp": signal.timestamp.isoformat(),
        })

        self.state["stats"]["total_signals"] += 1
        self.state["stats"]["last_signal"] = {
            "symbol": signal.symbol,
            "action": signal.action,
            "price": signal.price,
            "timestamp": signal.timestamp.isoformat(),
        }
        self.save()

    def set_last_scan(self):
        """Record the last scan time"""
        self.state["last_scan_time"] = datetime.utcnow().isoformat()
        self.state["stats"]["total_scans"] += 1
        self.save()

    def record_scan_history(self, snapshot: Dict[str, Any]) -> None:
        """Append a scan snapshot (e.g. a mid-cap universe scan) to persisted history.
        Kept under max_scan_history so the JSON file stays small."""
        scan_hist = self.state.setdefault("scan_history", [])
        scan_hist.append(snapshot)
        if len(scan_hist) > self.max_scan_history:
            self.state["scan_history"] = scan_hist[-self.max_scan_history:]
        self.save()

    def get_scan_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the latest persisted scan snapshots (oldest first if ascending)."""
        return self.state.get("scan_history", [])[-limit:]

    def get_signal_count(self, symbol: str, window_minutes: int = 60) -> int:
        """Count signals for a symbol in a time window"""
        cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
        count = 0
        for entry in self.state["signal_history"]:
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                if entry["symbol"] == symbol and ts > cutoff:
                    count += 1
            except (ValueError, KeyError):
                continue
        return count

    def get_stats(self) -> Dict[str, Any]:
        """Get bot statistics"""
        return self.state["stats"]

    def prune_old_signals(self, days: int = 30) -> None:
        """Remove signals older than N days"""
        cutoff = datetime.utcnow() - timedelta(days=days)
        before = len(self.state["signal_history"])
        self.state["signal_history"] = [
            e for e in self.state["signal_history"]
            if datetime.fromisoformat(e["timestamp"]) > cutoff
        ]
        if len(self.state["signal_history"]) != before:
            logger.info(f"Pruned {before - len(self.state['signal_history'])} old signals")
            self.save()

    # ------------------------------------------------------------------
    # Report deduplication (weekly / daily reports)
    # ------------------------------------------------------------------

    def is_report_sent(self, key: str) -> bool:
        """Check if a report for a given key (iso-week / date) was already sent"""
        return key in self.state.get("reports_sent", {})

    def is_smallcap_setup_sent(self, date_key: str) -> bool:
        """Check if today's small-cap setup report was already sent"""
        return date_key in self.state.get("smallcap_setup_sent", {})

    def record_smallcap_setup_sent(self, date_key: str) -> None:
        """Mark today's small-cap setup report as sent"""
        self.state.setdefault("smallcap_setup_sent", {})[date_key] = datetime.utcnow().isoformat()
        keys = list(self.state["smallcap_setup_sent"].keys())
        for k in keys[:-30]:
            del self.state["smallcap_setup_sent"][k]
        self.save()

    def is_smallcap_alert_sent(self, symbol: str, date_key: str) -> bool:
        """Check if a small-cap breakout alarm was already sent for this symbol today"""
        key = f"{symbol}:{date_key}"
        return key in self.state.get("smallcap_alerts_sent", {})

    def record_smallcap_alert_sent(self, symbol: str, date_key: str) -> None:
        """Mark a small-cap breakout alarm as sent"""
        self.state.setdefault("smallcap_alerts_sent", {})[f"{symbol}:{date_key}"] = datetime.utcnow().isoformat()
        self.save()

    def is_smallcap_predictions_sent(self, date_key: str) -> bool:
        """Check if today's 'tomorrow predictions' report was already sent"""
        return date_key in self.state.get("smallcap_predictions_sent", {})

    def is_smallcap_premarket_sent(self, date_key: str) -> bool:
        """Check if today's 'BUGÜN İZLE' pre-market report was already sent"""
        return date_key in self.state.get("smallcap_premarket_sent", {})

    def record_smallcap_premarket_sent(self, date_key: str) -> None:
        """Mark today's 'BUGÜN İZLE' pre-market report as sent"""
        self.state.setdefault("smallcap_premarket_sent", {})[date_key] = datetime.utcnow().isoformat()
        keys = list(self.state["smallcap_premarket_sent"].keys())
        for k in keys[:-30]:
            del self.state["smallcap_premarket_sent"][k]
        self.save()

    def record_smallcap_predictions_sent(self, date_key: str) -> None:
        """Mark today's 'tomorrow predictions' report as sent"""
        self.state.setdefault("smallcap_predictions_sent", {})[date_key] = datetime.utcnow().isoformat()
        keys = list(self.state["smallcap_predictions_sent"].keys())
        for k in keys[:-30]:
            del self.state["smallcap_predictions_sent"][k]
        self.save()

    def record_report(self, key: str, summary: Optional[Dict[str, Any]] = None) -> None:
        """Record a sent report to prevent duplicates across restarts"""
        self.state.setdefault("reports_sent", {})[key] = datetime.utcnow().isoformat()
        if summary:
            self.state.setdefault("weekly_reports", []).append(summary)
            max_hist = self.config.get("state", {}).get("max_signal_history", 1000)
            self.state["weekly_reports"] = self.state["weekly_reports"][-max_hist:]
        self.save()

    # ------------------------------------------------------------------
    # Prediction tracker (live backtest of small-cap recommendations)
    # ------------------------------------------------------------------

    def update_prediction_tracker(self, candidates: List[Dict[str, Any]], source: str = "daily") -> None:
        """Track each recommended candidate's limit/target/stop against later prices.

        Every scan we walk the latest candidate dicts (which include trade_plan)
        and record whether each symbol has broken out (price >= limit), hit its
        target, or stopped out. Used to compute a live hit-rate metric.

        `source` distinguishes "daily" small-cap candidates (entry/target/stop from
        trade_plan) from "weekly" breakout candidates (target/stop derived from ATR%).
        Track keys are namespaced so both can be tracked independently.
        """
        track = self.state.setdefault("prediction_tracker", {})
        now = datetime.utcnow().isoformat()

        for d in candidates:
            sym = d.get("symbol")
            if not sym:
                continue
            price = d.get("price") or 0
            tp = d.get("trade_plan") or {}
            limit = tp.get("limit") or 0
            target = tp.get("target") or 0
            stop = tp.get("stop") or 0

            if source == "weekly":
                # Haftalık adaylarda entry planı yok; beklenen haftalık hareket
                # (ATR%) etrafında simetrik hedef/stop kur.
                atr = float(d.get("atr_pct") or 0)
                if atr <= 0 or price <= 0:
                    continue
                limit = price
                target = price * (1 + atr / 100.0)
                stop = price * (1 - atr / 100.0)
            elif limit <= 0 or price <= 0:
                continue

            key = f"{source}:{sym}"
            rec = track.get(key)
            if rec is None:
                track[key] = {
                    "source": source,
                    "symbol": sym,
                    "first_seen": now,
                    "first_price": price,
                    "limit": limit,
                    "target": target,
                    "stop": stop,
                    "status": "open",          # open | breakout | target_hit | stopped_out | expired
                    "outcome": None,           # None | breakout | hit | stop | expired
                    "breakout_at": None,
                    "resolved_at": None,
                }
                continue

            if rec.get("status") not in ("open", "breakout"):
                continue

            # Kayıt anındaki sabit seviyelerle karşılaştır (her taramada güncel
            # fiyatla yeniden hesaplanan limit/target/stop ile değil).
            r_limit = rec.get("limit") or 0
            r_target = rec.get("target") or 0
            r_stop = rec.get("stop") or 0
            r_first = rec.get("first_price") or 0

            # breakout occurred: price crossed above the entry limit
            if rec["status"] == "open":
                if source == "weekly":
                    # Weekly adayda limit = ilk fiyat olduğundan "çıkış",
                    # fiyatın ilk fiyatın üzerine çıkmasıdır.
                    crossed = price > r_first
                else:
                    crossed = price >= r_limit and r_first < r_limit
                if crossed:
                    rec["status"] = "breakout"
                    rec["outcome"] = "breakout"
                    rec["breakout_at"] = now

            if rec["status"] == "breakout":
                if r_target > 0 and price >= r_target:
                    rec["status"] = "target_hit"
                    rec["outcome"] = "hit"
                    rec["resolved_at"] = now
                elif r_stop > 0 and price <= r_stop:
                    rec["status"] = "stopped_out"
                    rec["outcome"] = "stop"
                    rec["resolved_at"] = now
            elif rec["status"] == "open":
                if r_stop > 0 and price <= r_stop:
                    rec["status"] = "stopped_out"
                    rec["outcome"] = "stop"
                    rec["resolved_at"] = now

        # expire open entries older than 7 days (setup stale)
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        for key in list(track.keys()):
            rec = track[key]
            if rec.get("status") == "open" and rec.get("first_seen", "") < cutoff:
                rec["status"] = "expired"
                rec["outcome"] = "expired"
                rec["resolved_at"] = datetime.utcnow().isoformat()

        self.save()

    def get_prediction_stats(self, source: Optional[str] = None) -> Dict[str, Any]:
        """Aggregate the live backtest: hit rate of recommended candidates.
        `source` optionally filters to a single source ("daily" / "weekly")."""
        track = self.state.get("prediction_tracker", {}) or {}
        if source is not None:
            prefix = f"{source}:"
            track = {k: v for k, v in track.items() if k.startswith(prefix)}
        total = len(track)
        if total == 0:
            return {
                "total": 0, "open": 0, "breakout": 0, "hit": 0,
                "stop": 0, "expired": 0, "hit_rate_pct": None,
            }
        counts = {"open": 0, "breakout": 0, "hit": 0, "stop": 0, "expired": 0}
        for rec in track.values():
            st = rec.get("status", "open")
            # normalize status names: target_hit counts as hit, stopped_out as stop
            key = "hit" if st == "target_hit" else ("stop" if st == "stopped_out" else st)
            counts[key] = counts.get(key, 0) + 1
        resolved = counts["breakout"] + counts["hit"] + counts["stop"] + counts["expired"]
        favorable = counts["breakout"] + counts["hit"]
        hit_rate = (favorable / resolved * 100.0) if resolved else None
        return {
            "total": total,
            "open": counts["open"],
            "breakout": counts["breakout"],
            "hit": counts["hit"],
            "stop": counts["stop"],
            "expired": counts["expired"],
            "hit_rate_pct": round(hit_rate, 1) if hit_rate is not None else None,
        }
