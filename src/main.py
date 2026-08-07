"""
NASDAQ Signal Bot - Main Entry Point
Runs a continuous scan loop on Render (7/24)
"""
import os
import sys
import asyncio
import json
from datetime import datetime
from typing import Dict, Any, Optional

import yaml
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import setup_logger, get_logger
from src.data.price_fetcher import PriceFetcher
from src.data.news_fetcher import NewsFetcher
from src.analysis.technical import TechnicalAnalyzer
from src.analysis.signal_engine import SignalEngine
from src.notifier.telegram_bot import TelegramNotifier
from src.state.manager import StateManager
from src.utils.helpers import is_market_open, async_retry

# Load environment variables
load_dotenv()

# Setup logging
log_level = os.getenv("LOG_LEVEL", "INFO")
log_file = os.getenv("LOG_FILE", "logs/bot.log")
setup_logger(log_level, log_file)
logger = get_logger("main")


def load_config() -> Dict[str, Any]:
    """Load YAML config and merge with environment variables"""
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "settings.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Merge environment variables
    config["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    config["TELEGRAM_CHAT_ID"] = os.getenv("TELEGRAM_CHAT_ID", "")
    config["NEWSAPI_KEY"] = os.getenv("NEWSAPI_KEY", "")
    config["ALPHA_VANTAGE_KEY"] = os.getenv("ALPHA_VANTAGE_KEY", "")
    config["FINNHUB_KEY"] = os.getenv("FINNHUB_KEY", "")

    # Parse symbols from env if set
    env_symbols = os.getenv("SYMBOLS", "")
    if env_symbols:
        config["symbols"] = [s.strip() for s in env_symbols.split(",") if s.strip()]

    # Parse timeframes from env if set
    env_tfs = os.getenv("TIMEFRAMES", "")
    if env_tfs:
        config["timeframes"] = [t.strip() for t in env_tfs.split(",") if t.strip()]

    return config


class SignalBot:
    """Main bot orchestrator"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.scan_interval = config.get("scanner", {}).get("interval_seconds", 60)

        # Components
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

    async def _send_startup_message(self):
        """Send a startup notification to Telegram"""
        try:
            symbols = ", ".join(self.config.get("symbols", []))
            tfs = ", ".join(self.config.get("timeframes", []))
            msg = (
                f"🤖 <b>NASDAQ Sinyal Botu Başlatıldı</b>\n"
                f"{'=' * 30}\n"
                f"📊 <b>Semboller:</b> {symbols}\n"
                f"⏱ <b>Periyotlar:</b> {tfs}\n"
                f"🔄 <b>Tarama:</b> her {self.scan_interval}s\n"
                f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"Tarama başlıyor... 🔎"
            )
            await self.notifier.send_text(msg)
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

    async def _scan_once(self) -> int:
        """Run a single scan cycle, returns number of signals sent"""
        logger.info("=== Scanning market ===")

        # 1. Fetch price data
        price_data = await self.price_fetcher.fetch_all()

        # 2. Fetch news (only every N minutes to respect API limits)
        news_data = {}
        try:
            news_data = await self.news_fetcher.fetch_all()
        except Exception as e:
            logger.error(f"News fetch failed: {e}")

        # 3. Analyze technical indicators
        # Convert PriceData dicts to dataframes for analyzer
        analysis_input = {}
        for symbol, tfs in price_data.items():
            analysis_input[symbol] = {}
            for tf, pd_obj in tfs.items():
                analysis_input[symbol][tf] = pd_obj.data

        snapshots = self.analyzer.analyze_all(analysis_input)

        # 4. Build news aggregates for each symbol
        news_aggregates = {}
        for symbol in self.config.get("symbols", []):
            news_aggregates[symbol] = self.news_fetcher.get_aggregate_sentiment(symbol)

        # 5. Generate signals
        signals = self.signal_engine.evaluate_all(snapshots, news_aggregates)

        # 6. Filter duplicates and send
        sent = 0
        for signal in signals:
            if self.state.is_signal_duplicate(signal.signal_id):
                continue
            if await self.notifier.send_signal(signal):
                self.state.record_signal(signal)
                sent += 1

        # 7. Update state
        self.state.set_last_scan()

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
            except Exception as e:
                logger.error(f"Scan cycle error: {e}")
                logger.exception(e)
                self.health_ok = False
            finally:
                await asyncio.sleep(self.scan_interval)

    async def stop(self):
        """Graceful shutdown"""
        self.is_running = False
        await self.notifier.close()
        await self.news_fetcher.close()
        self.state.save()
        logger.info("Bot stopped gracefully")


async def main():
    """Entry point"""
    config = load_config()
    bot = SignalBot(config)

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        await bot.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.exception(e)
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
