"""
Confluence Risk Analysis Engine
- Wall Street-style risk manager format analysis
- Technical + confluence scoring output
- FastAPI compatible, async/await
"""
import math
from typing import Dict, Any, Tuple


class ConfluenceAnalyzer:
    """Generates Wall Street risk manager style analysis for algorithmic signals."""

    def __init__(self, data: Dict[str, Any]):
        self.symbol: str = data.get("symbol", "")
        self.price: float = float(data.get("price", 0))
        self.rsi: float = float(data.get("rsi", 50))
        self.macd: float = float(data.get("macd", 0))
        self.macd_signal: float = float(data.get("macd_signal", 0))
        self.vol_ratio: float = float(data.get("vol_ratio", 1.0))
        self.rs_score: float = float(data.get("rs_score", 0.0))

    def _bull_scenario(self) -> str:
        """Generate bull scenario (max 2 sentences, no speculative language)."""
        sentences = []

        # Trend/momentum basis
        if self.rsi > 55 and self.macd > self.macd_signal and self.vol_ratio >= 2.0:
            sentences.append(
                "Price RSI above 55 and MACD positive buy signal, showing trend support."
            )
            if self.vol_ratio >= 3.0:
                sentences.append(
                    "Daily volume 3x average, breakout with stronger continuation likelihood."
                )
        elif self.rsi > 55 and self.macd > self.macd_signal:
            sentences.append(
                "Price RSI above 55 and MACD positive buy signal, trend is supported."
            )
        elif self.vol_ratio >= 2.0:
            sentences.append(
                "Daily volume spike, technical strong confirmation of price movement."
            )

        # RS strength addition
        if self.rs_score > 1.2 and len(sentences) < 2:
            sentences.append(
                f"Relative strength score {self.rs_score:.1f} shows strong outperformance vs QQQ."
            )

        # Pad to exactly 2 sentences if needed
        while len(sentences) < 2:
            sentences.append("Market conditions support current technical levels.")

        return " ".join(sentences)

    def _bear_scenario(self) -> str:
        """Generate risk/trap scenario (max 2 sentences, no speculative language)."""
        sentences = []

        # Risk factors
        risk_parts = []

        if self.rsi > 70:
            risk_parts.append("RSI above 70, overbought region, reversal risk on upside.")
        elif self.rsi < 30:
            risk_parts.append("RSI below 30, oversold region, reversal risk on downside.")

        if self.macd < self.macd_signal:
            risk_parts.append("MACD below signal line, momentum reversal signal shown.")

        if self.vol_ratio < 1.5:
            risk_parts.append("No volume breakout, technical confirmation missing for breakout.")

        if self.rs_score < 1.0:
            risk_parts.append(f"Relative strength score {self.rs_score:.1f} weak vs QQQ, direction uncertain.")

        if risk_parts:
            sentences.append(" ".join(risk_parts[:2]))
        else:
            sentences.append("Indicators are balanced but immediate directional strength is weak.")

        # Pad to exactly 2 sentences if needed
        while len(sentences) < 2:
            sentences.append("Insufficient confirmation, false breakout probability increased.")

        return " ".join(sentences)

    def _calculate_stop_loss(self) -> Tuple[float, str]:
        """
        Calculate stop-loss based on technical levels.
        Uses volatility-adjusted percentage based on RSI and volume conditions.
        Returns (stop_price, reasoning_text).
        """
        # Base volatility percentage
        if self.rsi > 70 or self.rsi < 30:
            base_pct = 2.5  # Wider stop in extended markets
        elif self.rsi > 55 or self.rsi < 45:
            base_pct = 2.0
        else:
            base_pct = 1.8  # Tighter range

        # Volume adjustment
        if self.vol_ratio >= 3.0:
            base_pct *= 0.9  # Tighter stop on high volume confirmation
        elif self.vol_ratio < 1.5:
            base_pct *= 1.15  # Wider stop on low volume

        # MACD adjustment
        if self.macd > self.macd_signal:
            base_pct *= 0.95  # Slightly tighter if momentum aligns
        else:
            base_pct *= 1.05  # Slightly wider if momentum misaligned

        # Cap the percentage
        base_pct = max(1.5, min(base_pct, 4.0))

        stop_price = self.price * (1 - base_pct / 100)

        # Reasoning
        reason = f"Stop-loss: {base_pct:.1f}% price decline, {base_pct:.1f}% protection from entry."
        if self.macd > self.macd_signal:
            reason += " Momentum supported by positive MACD."
        if self.rsi > 70:
            reason += " Volatility premium calculated due to RSI overbought."

        return round(stop_price, 2), reason

    def _confidence_level(self) -> str:
        """Determine confidence: High / Medium / Low"""
        score = 0

        # RS score contribution
        if self.rs_score > 1.3:
            score += 3
        elif self.rs_score > 1.1:
            score += 2
        elif self.rs_score > 1.0:
            score += 1

        # RSI contribution
        if 55 <= self.rsi <= 65:
            score += 3
        elif 50 <= self.rsi <= 70 or 40 <= self.rsi <= 54:
            score += 2
        elif 30 <= self.rsi <= 49 or 71 <= self.rsi <= 80:
            score += 1

        # MACD contribution
        if self.macd > self.macd_signal:
            score += 3
        elif self.macd < self.macd_signal:
            score -= 1

        # Volume contribution
        if self.vol_ratio >= 3.0:
            score += 3
        elif self.vol_ratio >= 2.0:
            score += 2
        elif self.vol_ratio >= 1.5:
            score += 1

        if score >= 8:
            return "High"
        elif score >= 4:
            return "Medium"
        else:
            return "Low"

    def analyze(self) -> Dict[str, Any]:
        """Main analysis method returning formatted risk manager output."""
        bull = self._bull_scenario()
        bear = self._bear_scenario()
        stop_price, stop_reason = self._calculate_stop_loss()
        confidence = self._confidence_level()

        # Ensure exactly 2 sentences per scenario (split by period, trim)
        bull_sentences = [s.strip() for s in bull.split(".") if s.strip()]
        bear_sentences = [s.strip() for s in bear.split(".") if s.strip()]
        bull_final = ". ".join(bull_sentences[:2])
        if not bull_final.endswith("."):
            bull_final += "."
        bear_final = ". ".join(bear_sentences[:2])
        if not bear_final.endswith("."):
            bear_final += "."

        result = {
            "symbol": self.symbol,
            "price": self.price,
            "analysis": {
                "bull_scenario": bull_final,
                "bear_scenario": bear_final,
                "stop_mathematics": {
                    "stop_price": stop_price,
                    "reasoning": stop_reason,
                },
                "confidence": confidence,
            },
        }

        return result


def format_risk_manager_output(analysis: Dict[str, Any]) -> str:
    """Format the analysis dict into the exact risk manager bullet format requested."""
    symbol = analysis["symbol"]
    fiyat = analysis["price"]
    a = analysis["analysis"]

    # Extract bull scenario - first sentence before period
    bull_text = a["bull_scenario"]
    # Take first sentence only (before first period)
    bull_first = bull_text.split(".")[0].strip()

    # Extract bear scenario - first sentence before period
    bear_text = a["bear_scenario"]
    bear_first = bear_text.split(".")[0].strip()

    lines = [
        f"RISK ANAL {symbol} Risk Analizi",
        f"Price: {fiyat:.2f} USD",
        "",
        "BULL SCENARIO:",
        f"   {bull_first}",
        "",
        "BEAR / RISK SCENARIO:",
        f"   {bear_first}",
        "",
        "STOP MATHEMATICS:",
        f"   {a['stop_mathematics']['reasoning']}",
        "",
        "CONFIDENCE:",
        f"   {a['confidence']}",
    ]

    return "\n".join(lines)