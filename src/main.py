"""
NASDAQ Signal Bot - CLI Worker Entry Point
Usage: python -m src.main
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger
from src.bot import SignalBot, load_config

logger = get_logger("main")


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
