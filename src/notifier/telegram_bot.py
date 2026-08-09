"""
Telegram Notifier
- Sends formatted signal messages via Telegram Bot API
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
import asyncio

from src.utils.logger import get_logger
from src.utils.timezone import format_turkey
from src.analysis.signal_engine import Signal

logger = get_logger(__name__)

# Action -> emoji, label (Turkish) and plain description
ACTION_STYLE = {
    "STRONG_BUY": ("🚀", "GÜÇLÜ AL", "Kuvvetli alış sinyali"),
    "BUY": ("📈", "AL", "Alış sinyali"),
    "WATCH": ("👀", "İZLE", "Dikkat, bekleniyor"),
    "SELL": ("📉", "SAT", "Satış sinyali"),
    "STRONG_SELL": ("🚨", "GÜÇLÜ SAT", "Kuvvetli satış sinyali"),
}

# Default human-readable symbol names (overridable via config symbol_names)
DEFAULT_SYMBOL_NAMES = {
    "NQ=F": "Nasdaq 100",
    "ES=F": "S&P 500",
    "YM=F": "Dow Jones",
    "RTY=F": "Russell 2000",
    "QQQ": "Nasdaq 100 ETF",
    "SPY": "S&P 500 ETF",
    "DIA": "Dow Jones ETF",
    "IWM": "Russell 2000 ETF",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet (Google)",
    "GOOG": "Alphabet (Google)",
    "META": "Meta",
    "NFLX": "Netflix",
    "AMD": "AMD",
    "INTC": "Intel",
    "JPM": "JPMorgan",
}


class TelegramNotifier:
    """Sends signals to Telegram"""

    def __init__(self, bot_token: str, chat_id: str, config: Optional[Dict[str, Any]] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.config = config or {}
        self.tg_config = self.config.get("telegram", {})
        self.symbol_names = {**DEFAULT_SYMBOL_NAMES, **self.config.get("symbol_names", {})}
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self.parse_mode = self.tg_config.get("parse_mode", "HTML")
        self.disable_preview = self.tg_config.get("disable_web_page_preview", True)
        self.chart_base_url = self.tg_config.get("chart_base_url", "https://www.tradingview.com/chart/?symbol=")
        self.include_chart = self.tg_config.get("include_chart_link", True)

        import httpx
        self.client = httpx.AsyncClient(timeout=15.0)

    def symbol_display_name(self, symbol: str) -> str:
        """Return a human-readable name for a symbol, falling back to the ticker"""
        return self.symbol_names.get(symbol, symbol)

    def timeframe_label(self, timeframe: str) -> str:
        """Map a timeframe like '1m'/'5m'/'15m'/'1h'/'1d' to Turkish"""
        labels = {
            "1m": "1 dakika",
            "5m": "5 dakika",
            "15m": "15 dakika",
            "30m": "30 dakika",
            "1h": "1 saat",
            "4h": "4 saat",
            "1d": "1 gün",
            "1w": "1 hafta",
            "1wk": "1 hafta",
            "1mo": "1 ay",
            "1M": "1 ay",
        }
        return labels.get(timeframe, timeframe)

    async def close(self):
        """Close the HTTP client"""
        if self.client:
            await self.client.aclose()

    def _format_risk_text(self, signal: Signal) -> str:
        """Format risk management section"""
        if signal.stop_loss is None or signal.take_profit_1 is None:
            return ""
        direction_word = "AL" if signal.direction == "LONG" else "SAT"
        sl_emoji = "🛑" if signal.direction == "LONG" else "🔺"
        tp_emoji = "🎯"
        return (
            f"🛑 <b>Zarar Kesen (Stop Loss):</b> {signal.stop_loss:,.2f}\n"
            f"🎯 <b>Hedef 1 (Take Profit):</b> {signal.take_profit_1:,.2f}\n"
            f"🎯 <b>Hedef 2 (Take Profit):</b> {signal.take_profit_2:,.2f}\n"
            f"⚖️ <b>Risk/Kazanç Oranı:</b> 1:{signal.risk_reward_ratio:.2f}"
        )

    def _format_reasons(self, signal: Signal) -> str:
        """Format reasons list"""
        if not signal.reasons:
            return ""
        lines = "\n".join(f"• {r}" for r in signal.reasons)
        return f"🔍 <b>Nedenler:</b>\n{lines}"

    def build_message(self, signal: Signal) -> str:
        """Build the full Telegram message HTML"""
        emoji, label, description = ACTION_STYLE.get(signal.action, ("ℹ️", signal.action, ""))
        name = self.symbol_display_name(signal.symbol)
        tf_label = self.timeframe_label(signal.timeframe)

        if signal.direction == "LONG":
            direction_text = "🟢 <b>YÖN:</b> YUKARI (LONG)"
        else:
            direction_text = "🔴 <b>YÖN:</b> AŞAĞI (SHORT)"

        # Header
        msg = (
            f"{emoji} <b>{name} — {label}</b>\n"
            f"{'═' * 30}\n"
            f"{description}\n\n"
            f"🏷 <b>Hisse:</b> {name} (<code>{signal.symbol}</code>)\n"
            f"💰 <b>Fiyat:</b> {signal.price:,.2f} USD\n"
            f"📈 <b>Değişim:</b> {signal.change_pct:+.2f}%\n"
            f"⏱ <b>Periyot:</b> {tf_label}\n"
            f"{direction_text}\n"
        )

        # Score info
        msg += (
            f"\n⚡ <b>Sinyal Gücü:</b> {signal.strength:+.1f} / 100\n"
            f"   📊 Teknik Skor: {signal.technical_score:+.1f}"
        )
        if signal.news_score != 0:
            msg += f"\n   📰 Haber Skoru: {signal.news_score:+.1f}"
        msg += "\n"

        # Reasons
        reasons = self._format_reasons(signal)
        if reasons:
            msg += f"\n{reasons}\n"

        # News headline
        if signal.news_headline:
            msg += f"\n📰 <b>Haber Başlığı:</b> {signal.news_headline[:120]}"
            if len(signal.news_headline) > 120:
                msg += "..."
            msg += "\n"

        # Risk levels
        risk = self._format_risk_text(signal)
        if risk:
            msg += f"\n{risk}\n"

        # Chart link
        if self.include_chart:
            symbol = signal.symbol.replace("=F", "")
            msg += f"\n📈 <a href='{self.chart_base_url}{symbol}'>TradingView Grafiğini Aç</a>\n"

        # Timestamp
        ts = format_turkey(signal.timestamp, "%Y-%m-%d %H:%M")
        msg += f"\n🕐 {ts} (Türkiye saati)\nID: <code>{signal.signal_id}</code>"

        return msg

    async def send_signal(self, signal: Signal) -> bool:
        """Send a signal message to Telegram"""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram credentials missing - signal not sent")
            return False

        message = self.build_message(signal)

        try:
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": self.parse_mode,
                "disable_web_page_preview": self.disable_preview,
            }
            resp = await self.client.post(f"{self.api_url}/sendMessage", json=payload)
            if resp.status_code == 200:
                logger.info(f"✅ Signal sent: {signal.symbol} {signal.action}")
                return True
            else:
                logger.error(f"Telegram send failed: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    async def send_text(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a raw text message to Telegram"""
        if not self.bot_token or not self.chat_id:
            return False
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": self.disable_preview,
            }
            resp = await self.client.post(f"{self.api_url}/sendMessage", json=payload)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Telegram send_text error: {e}")
            return False

    async def send_many(self, signals: List[Signal]) -> int:
        """Send multiple signals, return count sent"""
        sent = 0
        for signal in signals:
            if await self.send_signal(signal):
                sent += 1
            await asyncio.sleep(1)  # Rate limit safety
        return sent

    def build_smallcap_setup_message(self, scanner, candidates=None, universe_size=0) -> str:
        """Build a small-cap setup report message from the scanner."""
        return scanner.build_setup_report(candidates or [], universe_size)

    async def send_smallcap_alert(self, text: str) -> bool:
        """Send a small-cap breakout alarm (HTML, no chunking).
        Alarms are short so one message is enough.
        """
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": self.parse_mode,
                "disable_web_page_preview": self.disable_preview,
            }
            resp = await self.client.post(f"{self.api_url}/sendMessage", json=payload)
            if resp.status_code == 200:
                logger.info("✅ Small-cap breakout alarm sent")
                return True
            logger.error(f"Small-cap alarm send failed: {resp.status_code} {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Small-cap alarm send error: {e}")
            return False

    async def send_report(self, text: str, chunk_size: int = 3800) -> bool:
        """Send a long report to Telegram, splitting into chunks under Telegram's limit.

        The report text may contain HTML tags (built by the report generator), so it
        is sent with HTML parse mode. Chunks are split on blank lines when possible.
        """
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram credentials missing - report not sent")
            return False

        text = text.strip()
        if not text:
            return False

        chunks: List[str] = []
        while len(text) > chunk_size:
            split_at = text.rfind("\n\n", 0, chunk_size)
            if split_at < chunk_size // 2:
                split_at = text.rfind("\n", 0, chunk_size)
            if split_at < chunk_size // 3:
                split_at = chunk_size
            chunks.append(text[:split_at].strip())
            text = text[split_at:].strip()
        if text:
            chunks.append(text)

        sent = 0
        for i, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                chunk = f"<i>📨 {i}/{len(chunks)}</i>\n\n" + chunk
            try:
                payload = {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": self.disable_preview,
                }
                resp = await self.client.post(f"{self.api_url}/sendMessage", json=payload)
                if resp.status_code == 200:
                    sent += 1
                else:
                    logger.error(f"Telegram report send failed: {resp.status_code} {resp.text}")
            except Exception as e:
                logger.error(f"Telegram report send error: {e}")
            await asyncio.sleep(1)
        return sent > 0
