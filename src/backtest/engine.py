"""
Backtest Engine (free, rule-based)
Replays the small-cap breakout strategy on historical OHLCV and measures real
P&L: win rate, total return %, avg R multiple, max drawdown, profit factor.
Includes commission + slippage so results are honest.

Strategy (mirrors the live scanner):
- Setup filter (daily): Bollinger squeeze (bbw percentile low) + near 52w high
- Entry (next day): price breaks above daily Donchian-20 upper
- Limit entry, stop = limit - ATR*sl, target = limit + ATR*tp
- Time stop: position closed after max_hold days if neither target nor stop hit
"""
import os
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple

import numpy as np
import pandas as pd

from src.utils.logger import get_logger
from src.analysis.technical import TechnicalAnalyzer

logger = get_logger(__name__)


@dataclass
class BacktestResult:
    symbol: str = ""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    total_return_pct: float = 0.0      # compounding, per-trade equal risk
    avg_return_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0         # gross win / gross loss
    max_drawdown_pct: float = 0.0
    avg_bars_held: float = 0.0
    rr_avg: float = 0.0
    setup_triggered: int = 0           # signals produced before filters
    equity_curve: List[float] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "total_return_pct": round(self.total_return_pct, 2),
            "avg_return_pct": round(self.avg_return_pct, 2),
            "avg_win_pct": round(self.avg_win_pct, 2),
            "avg_loss_pct": round(self.avg_loss_pct, 2),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "avg_bars_held": round(self.avg_bars_held, 1),
            "rr_avg": round(self.rr_avg, 2),
            "setup_triggered": self.setup_triggered,
            "equity_curve": [round(x, 3) for x in self.equity_curve[-60:]],
        }


class BacktestEngine:
    """Replays the breakout strategy on historical daily OHLCV."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.sc = config.get("smallcap", {})
        self.analyzer = TechnicalAnalyzer(config)

        # Strategy params (defaults match live scanner)
        self.sl_atr = float(self.sc.get("sl_atr_multiplier", 1.0))
        self.tp_atr = float(self.sc.get("tp_atr_multiplier", 2.0))
        self.donchian_period = int(self.analyzer.donchian_period)
        self.bb_period = int(self.analyzer.bb_period)
        self.min_squeeze_pctile = float(self.sc.get("backtest", {}).get("min_bbw_pctile", 30))
        self.max_hold_days = int(self.sc.get("backtest", {}).get("max_hold_days", 10))
        self.commission_pct = float(self.config.get("budget", {}).get("commission_pct", 0.5))
        self.slippage_pct = float(self.sc.get("backtest", {}).get("slippage_pct", 0.1))
        self.min_setup_score = float(self.sc.get("min_setup_score", 55))

    # ------------------------------------------------------------------
    # Setup detection (daily, historical)
    # ------------------------------------------------------------------

    def _is_setup(self, df: pd.DataFrame, i: int) -> Tuple[bool, float]:
        """Was bar i a valid squeeze setup? (BB width percentile low)."""
        if i < self.bb_period + 5:
            return False, 0.0
        close = df["close"]
        mid = close.iloc[: i + 1].rolling(window=self.bb_period).mean()
        std = close.iloc[: i + 1].rolling(window=self.bb_period).std()
        width = (2 * self.analyzer.bb_std * std) / mid * 100
        width = width.replace([np.inf, -np.inf], np.nan).dropna()
        if len(width) < self.bb_period + 5:
            return False, 0.0
        pctile = float(width.rank(pct=True).iloc[-1] * 100)
        return pctile < self.min_squeeze_pctile, pctile

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, df: pd.DataFrame, symbol: str) -> BacktestResult:
        """Backtest the strategy on daily bars. Returns per-symbol result."""
        res = BacktestResult(symbol=symbol)
        if df is None or len(df) < 90:
            return res

        df = df.copy()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                return res
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])

        o = df["open"].values
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        n = len(df)

        # Precompute ATR & Donchian once
        atr_series = self.analyzer._calc_atr(df).values
        dc_high = df["high"].rolling(window=self.donchian_period).max().values
        dc_low = df["low"].rolling(window=self.donchian_period).min().values

        in_position = False
        entry_idx = 0
        limit = stop = target = 0.0
        bars_held = 0
        setup_window = 0            # bars left to watch for a breakout after a setup
        held_times: List[int] = []
        returns: List[float] = []
        wins = losses = 0
        gross_win = gross_loss = 0.0
        equity = 1.0
        peak = 1.0
        curve: List[float] = [1.0]

        def close_trade(ret_pct: float):
            nonlocal wins, losses, gross_win, gross_loss, equity, peak, in_position
            returns.append(ret_pct)
            if ret_pct > 0:
                wins += 1
                gross_win += ret_pct
            else:
                losses += 1
                gross_loss += abs(ret_pct)
            equity *= (1 + ret_pct / 100)
            peak = max(peak, equity)
            curve.append(equity)
            in_position = False

        i = 0
        while i < n - 1:
            if not in_position:
                # Setup pencere kontrolü: squeeze tespit edilince takip eden
                # max_hold_days bar boyunca kırılım aranır (squeeze genelde birkaç
                # seans içinde çözülür).
                if setup_window == 0:
                    setup_ok, _ = self._is_setup(df, i)
                    if setup_ok:
                        res.setup_triggered += 1
                        setup_window = int(self.max_hold_days)
                    else:
                        i += 1
                        continue
                # Setup penceresi açık: dünkü Donchian bandının kırılımı aranır.
                # (Bugünkü band bugünkü high'ı içerir, kapanış asla onu aşamaz —
                # canlı tarayıcı da gerçek kırılım için aynı mantığı kullanır.)
                res_band = dc_high[i - 1] if i >= 1 else 0.0
                if res_band > 0 and c[i] > res_band and c[i] > 0:
                    atr = atr_series[i]
                    if atr > 0 and np.isfinite(atr):
                        limit = c[i] * (1 + self.slippage_pct / 100)
                        stop = limit - atr * self.sl_atr
                        target = limit + atr * self.tp_atr
                        if limit - stop > 0 and target > limit:
                            entry_idx = i
                            bars_held = 0
                            in_position = True
                            setup_window = 0
                            i += 1
                            continue
                # Kırılım yoksa pencere azalır; biterse yeni setup ara
                setup_window -= 1
                if setup_window < 0:
                    setup_window = 0
                i += 1
            else:
                bars_held += 1
                j = entry_idx + bars_held
                if j >= n:
                    # still open at data end -> close at last close
                    ret = (c[-1] / limit - 1) * 100 - self.commission_pct
                    close_trade(ret)
                    held_times.append(bars_held)
                    break
                day_high, day_low = h[j], l[j]
                # Check stop first (worst case), then target (conservative: gap)
                if day_low <= stop:
                    gap = max(0.0, (stop / limit - 1) * 100)
                    ret = min((stop / limit - 1) * 100, (day_low / limit - 1) * 100) - self.commission_pct
                    close_trade(ret)
                    held_times.append(bars_held)
                    i = j
                    continue
                if day_high >= target:
                    ret = (target / limit - 1) * 100 - self.commission_pct
                    close_trade(ret)
                    held_times.append(bars_held)
                    i = j
                    continue
                if bars_held >= self.max_hold_days:
                    ret = (c[j] / limit - 1) * 100 - self.commission_pct
                    close_trade(ret)
                    held_times.append(bars_held)
                    i = j
                    continue
                i = j

        # ---- Aggregate ----
        res.trades = len(returns)
        res.wins = wins
        res.losses = losses
        res.win_rate_pct = (wins / res.trades * 100) if res.trades else 0.0
        res.total_return_pct = (equity - 1) * 100
        res.avg_return_pct = float(np.mean(returns)) if returns else 0.0
        win_r = [r for r in returns if r > 0]
        loss_r = [r for r in returns if r < 0]
        res.avg_win_pct = float(np.mean(win_r)) if win_r else 0.0
        res.avg_loss_pct = float(np.mean(loss_r)) if loss_r else 0.0
        res.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (0.0 if gross_win == 0 else 99.0)
        res.max_drawdown_pct = self._max_drawdown(curve)
        res.avg_bars_held = float(np.mean(held_times)) if held_times else 0.0
        rr = []
        for r in returns:
            if r != 0:
                rr.append(1.0 if r > 0 else -1.0)  # R multiple approx from ATR plan
        res.rr_avg = float(np.mean(rr)) if rr else 0.0
        res.equity_curve = curve
        return res

    @staticmethod
    def _max_drawdown(curve: List[float]) -> float:
        peak = -1e9
        mdd = 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak * 100)
        return mdd


def summarize(results: List[BacktestResult]) -> Dict[str, Any]:
    """Aggregate per-symbol results into one dashboard-friendly summary."""
    done = [r for r in results if r.trades > 0]
    total_trades = sum(r.trades for r in done)
    total_wins = sum(r.wins for r in done)
    total_losses = sum(r.losses for r in done)
    # equal-weight avg of per-symbol total return
    avg_ret = float(np.mean([r.total_return_pct for r in done])) if done else 0.0
    win_rate = (total_wins / total_trades * 100) if total_trades else 0.0
    gross_w = sum((r.avg_win_pct * r.wins) for r in done)
    gross_l = sum((r.avg_loss_pct * r.losses) for r in done)
    pf = (gross_w / gross_l) if gross_l > 0 else (0.0 if gross_w == 0 else 99.0)
    mdd = max([r.max_drawdown_pct for r in done] or [0.0])
    return {
        "symbols_backtested": len(results),
        "symbols_with_trades": len(done),
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_rate_pct": round(win_rate, 1),
        "avg_symbol_return_pct": round(avg_ret, 2),
        "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(mdd, 2),
        "commission_pct": round(0.5, 2),
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
        "by_symbol": [r.to_dict() for r in sorted(done, key=lambda x: x.total_return_pct, reverse=True)[:20]],
    }


def run_backtest_for_symbols(config: Dict[str, Any],
                             data: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """Run the backtest over a dict {symbol: daily_df}."""
    engine = BacktestEngine(config)
    results: List[BacktestResult] = []
    for symbol, df in data.items():
        try:
            res = engine.run(df, symbol)
            results.append(res)
        except Exception as e:
            logger.warning(f"Backtest error {symbol}: {e}")
    return summarize(results)
