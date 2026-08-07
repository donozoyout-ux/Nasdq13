"""
Bot orchestrator - reusable by both CLI worker and web app
"""
import os
import sys
import asyncio
import yaml
from datetime import datetime
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import setup_logger, get_logger
from src.utils.timezone import market_status
from src.data.price_fetcher import PriceFetcher
from src.data.news_fetcher import NewsFetcher
from src.analysis.technical import TechnicalAnalyzer, IndicatorSnapshot
from src.analysis.signal_engine import SignalEngine, Signal
from src.notifier.telegram_bot import TelegramNotifier
from src.state.manager import StateManager

load_dotenv()

log_level = os.getenv("LOG_LEVEL", "INFO")
log_file = os.getenv("LOG_FILE", "logs/bot.log")
setup_logger(log_level, log_file)
logger = get_logger("bot")


def load_config() -> Dict[str, Any]:
    """Load YAML config and merge with environment variables"""
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "settings.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    config["TELEGRAM_CHAT_ID"] = os.getenv("TELEGRAM_CHAT_ID", "")
    config["NEWSAPI_KEY"] = os.getenv("NEWSAPI_KEY", "")
    config["ALPHA_VANTAGE_KEY"] = os.getenv("ALPHA_VANTAGE_KEY", "")
    config["FINNHUB_KEY"] = os.getenv("FINNHUB_KEY", "")

    env_symbols = os.getenv("SYMBOLS", "")
    if env_symbols:
        config["symbols"] = [s.strip() for s in env_symbols.split(",") if s.strip()]

    env_tfs = os.getenv("TIMEFRAMES", "")
    if env_tfs:
        config["timeframes"] = [t.strip() for t in env_tfs.split(",") if t.strip()]

    return config


class SignalBot:
    """Main bot orchestrator - runs scan loop, exposes state for dashboard"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.scan_interval = config.get("scanner", {}).get("interval_seconds", 60)

        self.price_fetcher = PriceFetcher(config)
        self.news_fetcher = NewsFetcher(config)
        self.analyzer = TechnicalAnalyzer(config)
        self.signal_engine = SignalEngine(config)
        self.state = StateManager(
            config.get("state", {}).get("file_path", "data/bot_state.json"),
            config,
        )
        self.notifier = TelegramNotifier(
            bot_token=config["TELEGRAM_BOT_TOKEN"],
            chat_id=config["TELEGRAM_CHAT_ID"],
            config=config,
        )

        self.is_running = False
        self.health_ok = True

        # Dashboard-visible state
        self.last_snapshots: Dict[str, Dict[str, IndicatorSnapshot]] = {}
        self.last_signals: List[Signal] = []
        self.last_news: Dict[str, Dict[str, Any]] = {}
        self.last_scan_at: Optional[datetime] = None
        self.scan_count = 0
        self.error_count = 0

    async def _send_startup_message(self):
        try:
            symbols = self.config.get("symbols", [])
            names = "".join(
                f"• {self.notifier.symbol_display_name(s)} (<code>{s}</code>)\n"
                for s in symbols
            )
            tfs = ", ".join(self.config.get("timeframes", []))
            status = market_status(self.config)
            session_text = {
                "regular": "🟢 Açık (normal seans)",
                "pre_market": "🌅 Pre-market",
                "after_hours": "🌙 After-hours",
                "closed": "🔴 Kapalı",
            }.get(status["session"], "🔴 Kapalı")
            now_tr = status["now_tr"].strftime("%Y-%m-%d %H:%M")
            msg = (
                f"🤖 <b>NASDAQ Sinyal Botu Başlatıldı</b>\n"
                f"{'=' * 30}\n"
                f"📊 <b>Takip Edilen Piyasalar:</b>\n{names}"
                f"⏱ <b>Periyotlar:</b> {tfs}\n"
                f"🔄 <b>Tarama:</b> her {self.scan_interval}s\n"
                f"🏙 <b>Piyasa:</b> {session_text}\n"
                f"🕐 {now_tr} (Türkiye saati)\n\n"
                f"Tarama başlıyor... 🔎"
            )
            await self.notifier.send_text(msg)
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

    async def _scan_once(self) -> int:
        """Run a single scan cycle, returns number of signals sent"""
        logger.info("=== Scanning market ===")

        price_data = await self.price_fetcher.fetch_all()

        news_data = {}
        try:
            news_data = await self.news_fetcher.fetch_all()
        except Exception as e:
            logger.error(f"News fetch failed: {e}")

        analysis_input = {}
        for symbol, tfs in price_data.items():
            analysis_input[symbol] = {}
            for tf, pd_obj in tfs.items():
                analysis_input[symbol][tf] = pd_obj.data

        snapshots = self.analyzer.analyze_all(analysis_input)
        self.last_snapshots = snapshots

        news_aggregates = {}
        for symbol in self.config.get("symbols", []):
            news_aggregates[symbol] = self.news_fetcher.get_aggregate_sentiment(symbol)
        self.last_news = news_aggregates

        signals = self.signal_engine.evaluate_all(snapshots, news_aggregates)
        self.last_signals = signals

        sent = 0
        for signal in signals:
            if self.state.is_signal_duplicate(signal.signal_id):
                continue
            if await self.notifier.send_signal(signal):
                self.state.record_signal(signal)
                sent += 1

        self.state.set_last_scan()
        self.last_scan_at = datetime.utcnow()
        self.scan_count += 1

        logger.info(f"=== Scan complete: {len(signals)} signals, {sent} sent ===")
        return sent

    async def run(self):
        """Main run loop"""
        self.is_running = True
        logger.info(f"🤖 Bot started with interval {self.scan_interval}s")
        await self._send_startup_message()

        while self.is_running:
            try:
                await self._scan_once()
                self.health_ok = True
            except Exception as e:
                logger.error(f"Scan cycle error: {e}")
                logger.exception(e)
                self.health_ok = False
                self.error_count += 1
            finally:
                await asyncio.sleep(self.scan_interval)

    async def stop(self):
        """Graceful shutdown"""
        self.is_running = False
        await self.notifier.close()
        await self.news_fetcher.close()
        self.state.save()
        logger.info("Bot stopped gracefully")

    # ------------------------------------------------------------------
    # Dashboard helpers
    # ------------------------------------------------------------------

    def snapshot_to_dict(self, snap: IndicatorSnapshot) -> Dict[str, Any]:
        """Convert a snapshot to a JSON-serializable dict"""
        import math

        def clean(v):
            if v is None:
                return 0
            try:
                f = float(v)
                return 0 if math.isnan(f) or math.isinf(f) else f
            except (ValueError, TypeError):
                return 0

        return {
            "symbol": snap.symbol,
            "timeframe": snap.timeframe,
            "price": clean(snap.price),
            "change_pct": round(clean(snap.change_pct), 2),
            "composite_score": round(clean(snap.composite_score), 1),
            "trend_score": round(clean(snap.trend_score), 1),
            "momentum_score": round(clean(snap.momentum_score), 1),
            "volume_score": round(clean(snap.volume_score), 1),
            "breakout_score": round(clean(snap.breakout_score), 1),
            "rsi_14": round(clean(snap.rsi_14), 1),
            "volume_ratio": round(clean(snap.volume_ratio), 2),
            "atr_14": round(clean(snap.atr_14), 2),
            "is_breakout_up": snap.is_breakout_up,
            "is_breakout_down": snap.is_breakout_down,
            "is_volume_spike": snap.is_volume_spike,
            "is_golden_cross": snap.is_golden_cross,
            "is_death_cross": snap.is_death_cross,
            "is_vwap_reclaim": snap.is_vwap_reclaim,
        }

    def get_dashboard_state(self) -> Dict[str, Any]:
        """Full state for the dashboard API"""
        symbols = self.config.get("symbols", [])
        timeframes = self.config.get("timeframes", [])

        market = {}
        for symbol in symbols:
            market[symbol] = {}
            tfs = self.last_snapshots.get(symbol, {})
            for tf in timeframes:
                snap = tfs.get(tf)
                if snap:
                    market[symbol][tf] = self.snapshot_to_dict(snap)
                else:
                    market[symbol][tf] = None

        signals = []
        for s in self.last_signals:
            signals.append({
                "id": s.signal_id,
                "symbol": s.symbol,
                "action": s.action,
                "direction": s.direction,
                "strength": round(s.strength, 1),
                "price": s.price,
                "timeframe": s.timeframe,
                "timestamp": s.timestamp.isoformat(),
                "reasons": s.reasons,
            })

        return {
            "status": {
                "running": self.is_running,
                "healthy": self.health_ok,
                "scan_count": self.scan_count,
                "error_count": self.error_count,
                "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
                "scan_interval_seconds": self.scan_interval,
            },
            "config": {
                "symbols": symbols,
                "timeframes": timeframes,
                "thresholds": self.config.get("thresholds", {}),
            },
            "market": market,
            "signals": signals,
            "news": self.last_news,
            "stats": self.state.get_stats(),
            "history": self.state.state.get("signal_history", [])[-20:],
        }
