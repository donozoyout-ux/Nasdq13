"""
AI Analyst - weekly & daily breakout candidate reports
- Default: FREE rule-based "AI" narrative (no API key needed)
- Optional: if OPENAI_API_KEY is set, uses a real LLM for richer text
"""
import os
import json
from datetime import datetime
from typing import Dict, List, Any, Optional

from src.utils.logger import get_logger
from src.utils.timezone import now_turkey
from src.analysis.weekly_screener import BreakoutCandidate, SETUP_TURKISH

logger = get_logger(__name__)


# Setup type -> narrative sentence templates (the "free AI" brain)
_SETUP_NARRATIVE = {
    "squeeze": (
        "Haftalık volatilite sıkışması yaşıyor; Bollinger bantları son {lookback} haftanın en dar seviyesinde. "
        "Geçmişte bu tür sıkışmalar genelde güçlü bir hareketle çözülür."
    ),
    "basing": (
        "52 haftalık zirvesine dayanmış durumda. Direncin kırılması halinde yeni zirve bölgesi serbest kalır "
        "ve hareket hızlanabilir."
    ),
    "trend": (
        "Haftalık EMA'lar yukarı dizilmiş durumda (EMA21 > EMA50). Güçlü bir yükseliş trendi içinde; "
        "geri çekilmeler alım fırsatı olarak görülebilir."
    ),
    "momentum": (
        "Momentum göstergeleri pozitif tarafta; MACD histogramı yükseliyor ve RSI sağlıklı bölgede. "
        "İvmenin korunması hareketi devam ettirebilir."
    ),
    "watch": (
        "Karışık sinyaller veriyor. Setup henüz tam netleşmedi; bir kırılım onayı beklenmeli."
    ),
}


_DAY_NAMES_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


class AiAnalyst:
    """Generates Turkish narrative reports (free rule-based, optional LLM)"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.ai_config = config.get("ai_report", {})
        self.enabled = self.ai_config.get("enabled", True)
        self.language = self.ai_config.get("language", "tr")
        self.llm_model = self.ai_config.get("llm_model", "gpt-4o-mini")
        self.max_tokens = int(self.ai_config.get("llm_max_tokens", 900))
        self.temperature = float(self.ai_config.get("llm_temperature", 0.4))
        self.include_disclaimer = self.ai_config.get("include_disclaimer", True)
        self.lookback = config.get("screener", {}).get("lookback_weeks", 60)

        # Provider: "gemini" (önerilen, ücretsiz AI Studio) | "openai"
        self.provider = os.getenv("AI_PROVIDER", "").strip().lower() or self.ai_config.get("provider", "gemini")
        self.llm_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        # Vision için model (görsel okuma) — Gemini free tier görüntü destekler
        self.vision_model = os.getenv("GEMINI_VISION_MODEL", "").strip() or "gemini-2.5-flash"
        self._http = None
        self.last_provider_used = "rule_based"

    def _has_llm(self) -> bool:
        if self.provider == "gemini":
            return bool(self.gemini_api_key)
        return bool(self.llm_api_key)

    async def _get_http(self):
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def close(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # LLM path (Gemini or OpenAI)
    # ------------------------------------------------------------------

    async def _llm_report(self, system_prompt: str, data_payload: Dict[str, Any]) -> Optional[str]:
        try:
            http = await self._get_http()
            if self.provider == "gemini":
                if not self.gemini_api_key:
                    return None
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{self.llm_model}:generateContent"
                )
                resp = await http.post(
                    url,
                    params={"key": self.gemini_api_key},
                    json={
                        "contents": [{
                            "parts": [
                                {"text": system_prompt + "\n\nVeriler:\n" + json.dumps(data_payload, ensure_ascii=False)},
                            ],
                        }],
                        "generationConfig": {
                            "maxOutputTokens": self.max_tokens,
                            "temperature": self.temperature,
                        },
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates and candidates[0].get("content", {}).get("parts"):
                        self.last_provider_used = "gemini"
                        return "".join(p.get("text", "") for p in candidates[0]["content"]["parts"]).strip()
                    logger.warning(f"Gemini empty response: {str(data)[:200]}")
                else:
                    logger.warning(f"Gemini report failed: {resp.status_code} {resp.text[:200]}")
            else:
                if not self.llm_api_key:
                    return None
                resp = await http.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.llm_api_key}"},
                    json={
                        "model": self.llm_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(data_payload, ensure_ascii=False)},
                        ],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self.last_provider_used = "openai"
                    return data["choices"][0]["message"]["content"].strip()
                logger.warning(f"LLM report failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"LLM report error: {e}")
        return None

    async def analyze_chart(self, symbol: str, chart_base64: str,
                            context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send a candlestick chart image to the vision model and get a Turkish
        chart read: patterns, S/R levels, VWAP position, verdict.
        Returns {comment, patterns, bias, disclaimer} or None (no key / failure)."""
        if not chart_base64 or not self._has_llm():
            return None
        try:
            http = await self._get_http()
            ctx = json.dumps(context, ensure_ascii=False, default=str)
            if self.provider == "gemini":
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{self.vision_model}:generateContent"
                )
                body = {
                    "contents": [{
                        "parts": [
                            {
                                "inline_data": {
                                    "mime_type": "image/png",
                                    "data": chart_base64,
                                }
                            },
                            {
                                "text": (
                                    "[SİSTEM/ROL TANIMI]\n"
                                    "Sen, mum çubukları analizi (Candlestick Analysis) üzerine uzmanlaşmış "
                                    "bir teknik analiz asistansın. Analizlerinde karmaşık formasyon isimlerini "
                                    "ezberlemek yerine 'Fiyat Davranışı' (Price Action) ve 'Mumların Mantığı' "
                                    "ilkelerini uygularsın.\n\n"
                                    "[TEMEL ANALİZ KURALLARI & STRATEJİ]\n"
                                    "1. MUM ANATOMİSİ VE KONTROL: Mumun renginden ziyade Gövde Büyüklüğü ve "
                                    "Kuyruk (Fitil) uzunluklarına odaklan. Uzun alt kuyruklar: alıcıların fiyatı "
                                    "yukarı ittiğini ve alt seviyelerin reddedildiğini gösterir (boğa baskısı). "
                                    "Uzun üst kuyruklar: satıcıların fiyatı aşağı bastırdığını ve üst seviyelerin "
                                    "reddedildiğini gösterir (ayı baskısı).\n"
                                    "2. MOMENTUM VE YUTAN MUM (ENGULFING): Önündeki bir veya birden fazla mumun "
                                    "gövdesini tamamen kaplayan büyük gövdeli mumları güçlü yön değişimi / "
                                    "momentum teyidi olarak kabul et.\n"
                                    "3. MUM BİRLEŞTİRME (CANDLE BLENDING): Birden fazla mumu analiz ederken ilk "
                                    "mumun AÇILIŞI, son mumun KAPANIŞI ile en yüksek/en düşük seviyeleri "
                                    "birleştirerek oluşan nihai tekil mumu hesapla ve piyasa yönünü buna göre yorumla.\n\n"
                                    "[GÖREV]\n"
                                    f"{symbol} için mum grafiğini yukarıdaki Mum Mantığı ve Candle Blending "
                                    "stratejisini kullanarak analiz et; alıcıların mı yoksa satıcıların mı üstün "
                                    "olduğunu ve olası yön beklentisini adım adım açıkla.\n\n"
                                    "Bağlam (kural tabanlı analiz sonucu):\n{ctx}\n\n"
                                    "Çıktı formatı: 1) alıcı mı satıcı mı üstün (tek satır) 2) yön beklentisi "
                                    "(YUKARI/AŞAĞI/NÖTR) 3) adım adım neden (gövde/fitil, engulfing, blended mum). "
                                    "Türkçe, max 6 satır, düz metin. Sonda 'yatırım tavsiyesi değildir' uyarısı ekle."
                                ),
                            },
                        ],
                    }],
                    "generationConfig": {"maxOutputTokens": 600, "temperature": 0.3},
                }
                resp = await http.post(url, params={"key": self.gemini_api_key}, json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    cands = data.get("candidates", [])
                    if cands and cands[0].get("content", {}).get("parts"):
                        text = "".join(p.get("text", "") for p in cands[0]["content"]["parts"]).strip()
                        self.last_provider_used = "gemini"
                        return {"comment": text, "bias": 0, "disclaimer": True}
                    logger.warning(f"Gemini vision empty: {str(data)[:200]}")
                else:
                    logger.warning(f"Gemini vision failed: {resp.status_code} {resp.text[:200]}")
            else:
                # OpenAI vision fallback (gpt-4o)
                resp = await http.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.llm_api_key}"},
                    json={
                        "model": "gpt-4o",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": f"{symbol} grafiğini analiz et. Bağlam: {ctx}. Türkçe, max 6 satır."},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{chart_base64}"}},
                            ],
                        }],
                        "max_tokens": 600,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self.last_provider_used = "openai"
                    return {"comment": data["choices"][0]["message"]["content"].strip(), "bias": 0, "disclaimer": True}
                logger.warning(f"OpenAI vision failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Chart analysis error {symbol}: {e}")
        return None

    # ------------------------------------------------------------------
    # Free rule-based brain
    # ------------------------------------------------------------------

    def _narrative_line(self, c: BreakoutCandidate) -> str:
        tmpl = _SETUP_NARRATIVE.get(c.setup_type)
        if not tmpl:
            tmpl = _SETUP_NARRATIVE["watch"]
        text = tmpl.format(lookback=self.lookback)
        if c.rs_4w > 2:
            text += f" S&P 500'e göre son 4 haftada +{c.rs_4w:.1f} puan üstün performans gösteriyor."
        if c.vol_ratio < 0.8:
            text += " Hacim daralması birikimi işaret ediyor; patlama için yakıt birikiyor."
        return text

    def _weekly_report_text(self, candidates: List[BreakoutCandidate], index_stats: Dict[str, Any]) -> str:
        now = now_turkey()
        n = len(candidates)
        scanned = index_stats.get("scanned", 0)
        title = "🤖 HAFTALIK ÇIKIŞ ADAYLARI"
        subtitle = f"📅 {now.strftime('%d.%m.%Y')} · {scanned} hisse tarandı, {n} aday seçildi"

        lines = [title, subtitle, "─" * 30]

        idx_trend = index_stats.get("trend", "neutral")
        idx_trend_text = {"bullish": "yükseliş", "bearish": "düşüş", "neutral": "yatay"}.get(idx_trend, "yatay")
        idx_ret = index_stats.get("return_4w")
        idx_str = f"{idx_ret:+.1f}%" if idx_ret is not None else "-"
        lines.append(f"📊 <b>S&P 500:</b> son 4 hafta {idx_str} (piyasa {idx_trend_text})")
        lines.append("")

        for i, c in enumerate(candidates, 1):
            setup = SETUP_TURKISH.get(c.setup_type, c.setup_type)
            lines.append(
                f"🏆 <b>{i}. {c.name} ({c.symbol})</b> — Çıkış Skoru: <b>{c.setup_score:.0f}/100</b>\n"
                f"   💰 Fiyat: <b>{c.price:,.2f} USD</b> | Haftalık: {c.change_pct:+.2f}%\n"
                f"   📐 Setup: <b>{setup}</b>\n"
                f"   📈 4H getiri: {c.weekly_return_4w:+.1f}% | 8H: {c.weekly_return_8w:+.1f}%\n"
                f"   🎯 52H zirveye mesafe: {c.dist_52w_high_pct:+.1f}%\n"
                f"   ⚡ Beklenen haftalık hareket: ±{c.atr_pct:.1f}%\n"
                f"   🔍 RSI: {c.rsi_14:.0f} | Hacim: {c.vol_ratio:.1f}x"
            )
            if c.reasons:
                lines.append("   • " + "\n   • ".join(c.reasons))
            lines.append(f"   💡 {self._narrative_line(c)}")
            lines.append("")

        if self.include_disclaimer:
            lines.append(
                "⚠️ <i>Bu rapor otomatik üretilmiştir ve yatırım tavsiyesi değildir. "
                "Ani hareketler her iki yönde de gerçekleşebilir.</i>"
            )

        return "\n".join(lines)

    def _daily_report_text(self, candidates: List[BreakoutCandidate], index_stats: Dict[str, Any]) -> str:
        now = now_turkey()
        day_name = _DAY_NAMES_TR[now.weekday()]
        lines = [
            "📈 <b>GÜNÜN ÇIKIŞ ADAYLARI</b>",
            f"📅 {day_name}, {now.strftime('%d.%m.%Y')}",
            "─" * 30,
            "Bu haftanın adayları arasından bugünkü tetikleyicileri en güçlü olanlar:",
            "",
        ]

        for i, c in enumerate(candidates[:8], 1):
            lines.append(
                f"{i}. <b>{c.symbol}</b> — Günlük Tetik: <b>{c.daily_score:.0f}/100</b> (Haftalık setup: {c.setup_score:.0f})\n"
                f"   💰 {c.price:,.2f} USD | {c.change_pct:+.2f}% | Hacim: {c.vol_ratio:.1f}x | RSI: {c.rsi_14:.0f}"
            )
            if c.daily_triggers:
                lines.append("   • " + "\n   • ".join(c.daily_triggers))

        lines.append("")
        if self.include_disclaimer:
            lines.append("⚠️ <i>Otomatik üretilmiştir, yatırım tavsiyesi değildir.</i>")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_weekly(self, candidates: List[BreakoutCandidate],
                              index_stats: Dict[str, Any]) -> str:
        if self.enabled and self._has_llm():
            prompt = (
                "Sen profesyonel bir teknik analist ve Türkçe yazan bir piyasa yazarısın. "
                "Sana haftalık teknik analiz sonuçları verilecek. Bunları yatırımcı dostu, "
                "kısa paragraflı, samimi ama nesnel bir Türkçe 'Haftalık Çıkış Adayları' raporuna çevir. "
                "Her hisse için: isim, sembol, skor, setup türü ve 2-3 cümle neden bu hafta hareket edebileceği. "
                "Sonda 'yatırım tavsiyesi değildir' uyarısı ekle. HTML etiketi kullanma, düz metin + emoji kullan."
            )
            payload = {
                "index": index_stats,
                "candidates": [c.to_dict() for c in candidates[:self.max_tokens // 30]],
            }
            llm_text = await self._llm_report(prompt, payload)
            if llm_text:
                return llm_text

        return self._weekly_report_text(candidates, index_stats)

    async def generate_daily(self, candidates: List[BreakoutCandidate],
                             index_stats: Dict[str, Any]) -> str:
        if self.enabled and self._has_llm():
            prompt = (
                "Sen profesyonel bir günlük trade analistisin. Türkçe yaz. "
                "Sana haftalık çıkış adayları ve günlük tetikleyicileri verilecek. "
                "Kısa bir 'Günün Çıkış Adayları' brifingi yaz: her hisse için 1-2 cümle "
                "neden bugün hareket edebileceği ve nelere dikkat edileceği. "
                "Sonda 'yatırım tavsiyesi değildir' uyarısı ekle. Düz metin + emoji kullan."
            )
            payload = {"candidates": [c.to_dict() for c in candidates[:8]]}
            llm_text = await self._llm_report(prompt, payload)
            if llm_text:
                return llm_text

        return self._daily_report_text(candidates, index_stats)
