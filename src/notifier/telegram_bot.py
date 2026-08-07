"""
Telegram Notifier
- Sends formatted signal messages via Telegram Bot API
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
import asyncio

from src.utils.logger import get_logger
from src.analysis.signal_engine import Signal

logger = get_logger(__name__)

# Action -> emoji and color mapping
ACTION_STYLE = {
    "STRONG_BUY": ("🚀", "🟢", "STRONG BUY"),
    "BUY": ("📈", "🟢", "BUY"),
    "WATCH": ("👀", "🟡", "WATCH"),
    "SELL": ("📉", "🔴", "SELL"),
    "STRONG_SELL": ("🚨", "🔴", "STRONG SELL"),
}


class TelegramNotifier:
    """Sends signals to Telegram"""

    def __init__(self, bot_token: str, chat_id: str, config: Optional[Dict[str, Any]] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.config = config or {}
        self.tg_config = self.config.get("telegram", {})
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self.parse_mode = self.tg_config.get("parse_mode", "HTML")
        self.disable_preview = self.tg_config.get("disable_web_page_preview", True)
        self.chart_base_url = self.tg_config.get("chart_base_url", "https://www.tradingview.com/chart/?symbol=")
        self.include_chart = self.tg_config.get("include_chart_link", True)

        import httpx
        self.client = httpx.AsyncClient(timeout=15.0)

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
            f"{sl_emoji} <b>Stop Loss:</b> {signal.stop_loss:,.2f}\n"
            f"{tp_emoji} <b>Take Profit 1:</b> {signal.take_profit_1:,.2f}\n"
            f"{tp_emoji} <b>Take Profit 2:</b> {signal.take_profit_2:,.2f}\n"
            f"⚖️ <b>Risk/Reward:</b> 1:{signal.risk_reward_ratio:.2f}"
        )

    def _format_reasons(self, signal: Signal) -> str:
        """Format reasons list"""
        if not signal.reasons:
            return ""
        lines = "\n".join(f"• {r}" for r in signal.reasons)
        return f"🔍 <b>Nedenler:</b>\n{lines}"

    def build_message(self, signal: Signal) -> str:
        """Build the full Telegram message HTML"""
        emoji, color, label = ACTION_STYLE.get(signal.action, ("ℹ️", "⚪", signal.action))

        # Header
        msg = (
            f"{emoji} <b>NASDAQ SİNYAL: {label}</b>\n"
            f"{'=' * 30}\n"
            f"📊 <b>Sembol:</b> <code>{signal.symbol}</code>\n"
            f"💰 <b>Fiyat:</b> {signal.price:,.2f}\n"
            f"📈 <b>Değişim:</b> {signal.change_pct:+.2f}%\n"
            f"⏱ <b>Periyot:</b> {signal.timeframe}\n"
        )

        # Score info
        msg += (
            f"\n⚡ <b>Skor:</b> {signal.strength:+.1f} "
            f"(Teknik: {signal.technical_score:+.1f}"
        )
        if signal.news_score != 0:
            msg += f" | Haber: {signal.news_score:+.1f})"
        else:
            msg += ")"
        msg += "\n"

        # Reasons
        reasons = self._format_reasons(signal)
        if reasons:
            msg += f"\n{reasons}\n"

        # News headline
        if signal.news_headline:
            msg += f"\n📰 <b>Haber:</b> {signal.news_headline[:120]}"
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
            msg += f"\n📈 <a href='{self.chart_base_url}{symbol}'>TradingView Grafiği</a>\n"

        # Timestamp
        ts = signal.timestamp.strftime("%Y-%m-%d %H:%M UTC")
        msg += f"\n🕐 {ts}\nID: <code>{signal.signal_id}</code>"

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
