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
            "last_scan_time": None,
            "stats": {
                "total_signals": 0,
                "total_scans": 0,
                "last_signal": None,
            },
            "created_at": datetime.utcnow().isoformat(),
        }
        self.max_history = self.config.get("state", {}).get("max_signal_history", 1000)
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

            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False, default=str)
            logger.debug(f"State saved to {self.file_path}")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

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
