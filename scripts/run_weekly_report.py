"""
Run the weekly breakout report NOW (market closed -> analysis time).
Usage:
  python -m scripts.run_weekly_report          # atla (bu hafta gönderilmediyse)
  python -m scripts.run_weekly_report --force  # zorla yeniden gönder
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import setup_logger
from src.bot import SignalBot, load_config

setup_logger("INFO", None)


async def main():
    force = "--force" in sys.argv
    config = load_config()
    bot = SignalBot(config)
    try:
        key = bot._weekly_report_key()
        if bot.state.is_report_sent(key) and not force:
            print(f"Bu haftanın raporu ({key}) zaten gönderilmiş. Yeniden göndermek için: --force")
            return
        ok = await bot.run_weekly_report()
        print("Rapor başarıyla üretildi ve gönderildi." if ok else "Rapor gönderilemedi.")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
