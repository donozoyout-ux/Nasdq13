"""Quick live test: fetch prices + run technical analysis + send test signal"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import setup_logger
from src.data.price_fetcher import PriceFetcher
from src.analysis.technical import TechnicalAnalyzer
from src.analysis.signal_engine import SignalEngine
from src.notifier.telegram_bot import TelegramNotifier

setup_logger("INFO", None)


async def main():
    config = {
        "symbols": ["NQ=F", "ES=F"],
        "timeframes": ["5m", "15m", "1h"],
        "scanner": {"max_concurrent_requests": 3, "request_timeout": 30},
        "technical": {},
        "signal_weights": {
            "breakout_strength": 30,
            "volume_confirmation": 25,
            "trend_alignment": 20,
            "momentum": 15,
            "support_resistance": 10,
        },
        "thresholds": {"strong_buy": 75, "buy": 60, "watch": 45, "strong_sell": -75, "sell": -60},
        "risk": {
            "atr_sl_multiplier": 1.5,
            "atr_tp1_multiplier": 2.0,
            "atr_tp2_multiplier": 3.0,
            "max_signals_per_hour": 10,
            "signal_cooldown_minutes": 15,
            "min_risk_reward_ratio": 1.0,
        },
        "news": {"keyword_boost_max": 30},
        "telegram": {
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "include_chart_link": True,
            "chart_base_url": "https://www.tradingview.com/chart/?symbol=",
        },
    }

    print("=== 1. Fiyat verisi cekiliyor ===")
    fetcher = PriceFetcher(config)
    price_data = await fetcher.fetch_all()

    analysis_input = {}
    for symbol, tfs in price_data.items():
        analysis_input[symbol] = {}
        for tf, pd_obj in tfs.items():
            analysis_input[symbol][tf] = pd_obj.data
            print(f"  {symbol} {tf}: {len(pd_obj.data)} bar")

    print("\n=== 2. Teknik analiz ===")
    analyzer = TechnicalAnalyzer(config)
    snapshots = analyzer.analyze_all(analysis_input)

    for symbol, tfs in snapshots.items():
        for tf, snap in tfs.items():
            print(f"  {symbol} {tf}: fiyat={snap.price:.2f} skor={snap.composite_score:+.1f} "
                  f"RSI={snap.rsi_14:.1f} hacim={snap.volume_ratio:.1f}x "
                  f"BO_UP={snap.is_breakout_up} BO_DOWN={snap.is_breakout_down}")

    print("\n=== 3. Sinyal degerlendirme ===")
    engine = SignalEngine(config)
    signals = engine.evaluate_all(snapshots)
    print(f"  {len(signals)} sinyal bulundu")

    print("\n=== 4. Telegram'a sinyal gonderimi ===")
    notifier = TelegramNotifier(
        bot_token="8888188361:AAG3PsWMoOzHMkGxmjBiQ0oLDvMExgoY9Z4",
        chat_id="8261250171",
        config=config,
    )

    if signals:
        sent = 0
        for s in signals:
            if await notifier.send_signal(s):
                sent += 1
        print(f"  {sent} sinyal Telegram'a gonderildi")
    else:
        print("  Sinif esigine ulasan yok - genel durum mesaji gonderilecek")
        # Send a summary message so user sees something in Telegram
        lines = []
        for symbol, tfs in snapshots.items():
            for tf, snap in tfs.items():
                direction = "YUKARI" if snap.composite_score > 50 else ("ASAGI" if snap.composite_score < 50 else "NÖTR")
                lines.append(
                    f"{symbol} ({tf}): fiyat {snap.price:,.2f} | skor {snap.composite_score:+.1f} | "
                    f"RSI {snap.rsi_14:.0f} | hacim {snap.volume_ratio:.1f}x | {direction}"
                )
        summary = "📊 <b>NASDAQ Durum Raporu (test)</b>\n\n" + "\n".join(lines)
        await notifier.send_text(summary)
        print("  Durum raporu gonderildi")

    await notifier.close()


if __name__ == "__main__":
    asyncio.run(main())
