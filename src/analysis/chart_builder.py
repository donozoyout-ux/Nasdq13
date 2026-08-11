"""
Chart Builder - renders a candlestick chart to a PNG (base64) for the AI vision
analyst to read. Uses matplotlib with a quiet, headless (Agg) backend so it
works on Render without a display.

Output is a JPEG/base64 data-URI that is sent to the Gemini vision model.
"""
import io
import base64
from typing import Optional, Any

import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    _MPL = True
except Exception as e:  # pragma: no cover
    _MPL = False
    logger.warning(f"matplotlib unavailable: {e}")


def build_candlestick_chart(df: pd.DataFrame, symbol: str,
                            timeframe: str = "1d", lookback: int = 90,
                            max_px: int = 1200) -> Optional[str]:
    """Render the last `lookback` candles as a PNG and return a base64 string
    (raw base64, no data-URI prefix). Returns None if rendering fails."""
    if not _MPL or df is None or len(df) < 20:
        return None

    try:
        df = df.copy()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        sub = df.tail(lookback).reset_index(drop=True)

        fig, (ax_p, ax_v) = plt.subplots(
            2, 1, figsize=(10, 6), sharex=True,
            gridspec_kw={"height_ratios": [4, 1]},
        )

        # --- Price panel: candles + EMA21/50 ---
        o = sub["open"].values
        h = sub["high"].values
        l = sub["low"].values
        c = sub["close"].values
        x = list(range(len(sub)))

        up = c >= o
        ax_p.bar(x, (h - l).clip(min=0), bottom=l, width=0.08,
                 color=["#26a69a" if u else "#ef5350" for u in up], alpha=0.6)
        for i in range(len(sub)):
            body_low = min(o[i], c[i])
            body_h = abs(c[i] - o[i]) or (h[i] - l[i]) * 0.03
            ax_p.add_patch(Rectangle(
                (i - 0.3, body_low), 0.6, body_h,
                facecolor="#26a69a" if up[i] else "#ef5350",
                edgecolor="#26a69a" if up[i] else "#ef5350",
                linewidth=0.5,
            ))

        ema21 = sub["close"].ewm(span=21, adjust=False).mean()
        ema50 = sub["close"].ewm(span=50, adjust=False).mean()
        ax_p.plot(x, ema21.values, color="#fbbf24", linewidth=1.0, label="EMA21")
        ax_p.plot(x, ema50.values, color="#60a5fa", linewidth=1.0, label="EMA50")
        ax_p.set_title(f"{symbol} — {timeframe} candlestick", fontsize=11, loc="left")
        ax_p.legend(loc="upper left", fontsize=8, framealpha=0.5)
        ax_p.grid(alpha=0.2)
        ax_p.set_ylabel("Fiyat", fontsize=8)

        # --- Volume panel ---
        vol = sub["volume"].values
        ax_v.bar(x, vol, color=["#26a69a" if u else "#ef5350" for u in up], width=0.8, alpha=0.7)
        ax_v.grid(alpha=0.2)
        ax_v.set_ylabel("Hacim", fontsize=8)

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=80)
        plt.close(fig)
        png = buf.getvalue()
        # Downscale if needed (Gemini token cost scales with image size)
        if len(png) > max_px * max_px // 2 and False:  # keep simple: fixed dpi
            pass
        return base64.b64encode(png).decode("utf-8")
    except Exception as e:
        logger.warning(f"Chart build error {symbol}: {e}")
        return None
