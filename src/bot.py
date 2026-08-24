"""
Bot orchestrator - reusable by both CLI worker and web app
"""
import os
import sys
import asyncio
import hashlib
import yaml
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import setup_logger, get_logger
from src.utils.timezone import market_status, now_turkey, premarket_report_due
from src.data.price_fetcher import PriceFetcher
from src.data.news_fetcher import NewsFetcher
from src.analysis.technical import TechnicalAnalyzer, IndicatorSnapshot
from src.analysis.signal_engine import SignalEngine, Signal
from src.analysis.weekly_screener import WeeklyScreener
from src.analysis.smallcap_scanner import SmallCapScanner
from src.analysis.ai_analyst import AiAnalyst
from src.backtest.engine import run_backtest_for_symbols
from src.notifier.telegram_bot import TelegramNotifier
from src.state.manager import StateManager
from src.utils.github_backup import GithubBackup
load_dotenv()

log_level = os.getenv("LOG_LEVEL", "INFO")
log_file = os.getenv("LOG_FILE", "logs/bot.log")
setup_logger(log_level, log_file)
logger = get_logger("bot")


def load_config() -> Dict[str, Any]:
    """Load YAML config and merge with environment variables"""
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "settings.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    config["TELEGRAM_CHAT_ID"] = os.getenv("TELEGRAM_CHAT_ID", "")
    config["NEWSAPI_KEY"] = os.getenv("NEWSAPI_KEY", "")
    config["ALPHA_VANTAGE_KEY"] = os.getenv("ALPHA_VANTAGE_KEY", "")
    config["FINNHUB_KEY"] = os.getenv("FINNHUB_KEY", "")

    env_symbols = os.getenv("SYMBOLS", "")
    if env_symbols:
        config["symbols"] = [s.strip() for s in env_symbols.split(",") if s.strip()]

    env_tfs = os.getenv("TIMEFRAMES", "")
    if env_tfs:
        config["timeframes"] = [t.strip() for t in env_tfs.split(",") if t.strip()]

    # Schedule overrides (Türkiye saati)
    sched = config.setdefault("schedule", {})
    wr = sched.setdefault("weekly_report", {})
    if os.getenv("WEEKLY_REPORT_DAY", ""):
        wr["day_of_week"] = int(os.getenv("WEEKLY_REPORT_DAY"))
    if os.getenv("WEEKLY_REPORT_HOUR", ""):
        wr["hour_tr"] = int(os.getenv("WEEKLY_REPORT_HOUR"))
    if os.getenv("WEEKLY_REPORT_MINUTE", ""):
        wr["minute_tr"] = int(os.getenv("WEEKLY_REPORT_MINUTE"))
    db = sched.setdefault("daily_brief", {})
    start = os.getenv("DAILY_BRIEF_DAY_START", "")
    end = os.getenv("DAILY_BRIEF_DAY_END", "")
    if start != "" and end != "":
        db["days"] = list(range(int(start), int(end) + 1))
    if os.getenv("DAILY_BRIEF_HOUR", ""):
        db["hour_tr"] = int(os.getenv("DAILY_BRIEF_HOUR"))
    if os.getenv("DAILY_BRIEF_MINUTE", ""):
        db["minute_tr"] = int(os.getenv("DAILY_BRIEF_MINUTE"))

    # Small-cap overrides (opsiyonel)
    sc = config.setdefault("smallcap", {})
    if os.getenv("SMALLCAP_ENABLED", ""):
        sc["enabled"] = os.getenv("SMALLCAP_ENABLED").lower() in ("1", "true", "yes")
    if os.getenv("SMALLCAP_SCAN_INTERVAL_MINUTES", ""):
        sc["scan_interval_minutes"] = int(os.getenv("SMALLCAP_SCAN_INTERVAL_MINUTES"))

    # Budget overrides (bütçe bazlı öneri)
    bgt = config.setdefault("budget", {})
    if os.getenv("BUDGET_TRY", ""):
        bgt["budget_try"] = float(os.getenv("BUDGET_TRY"))
    if os.getenv("USD_TRY_RATE", ""):
        bgt["usd_try_rate"] = float(os.getenv("USD_TRY_RATE"))

    return config


class SignalBot:
    """Main bot orchestrator - runs scan loop, exposes state for dashboard"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.scan_interval = config.get("scanner", {}).get("interval_seconds", 60)

        self.price_fetcher = PriceFetcher(config)
        self.news_fetcher = NewsFetcher(config)
        self.analyzer = TechnicalAnalyzer(config)
        self.signal_engine = SignalEngine(config)
        self.screener = WeeklyScreener(config)
        self.ai_analyst = AiAnalyst(config)
        self.smallcap_scanner = SmallCapScanner(config) if config.get("smallcap", {}).get("enabled", True) else None
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

        # Dashboard-visible state
        self.last_snapshots: Dict[str, Dict[str, IndicatorSnapshot]] = {}
        self.last_signals: List[Signal] = []
        self.last_news: Dict[str, Dict[str, Any]] = {}
        self.last_scan_at: Optional[datetime] = None
        self.scan_count = 0
        self.error_count = 0

        # Reports
        self.last_weekly_report: Optional[Dict[str, Any]] = None
        self.last_daily_brief: Optional[Dict[str, Any]] = None

        # Small-cap scanner state
        self.smallcap_last_scan: Optional[datetime] = None
        self.smallcap_candidates: List[Dict[str, Any]] = []
        self.smallcap_universe_size = 0

        # AI chart analyses (vision) — top candidates only, budget-capped
        self.last_chart_analyses: Dict[str, Dict[str, Any]] = {}
        self._chart_analysis_day: Optional[str] = None
        self._chart_analysis_budget = 0

        # GitHub backup (archives scans/signals to a private repo branch)
        self.github_backup = GithubBackup(config)
        self.backup_status: Dict[str, Any] = {
            "enabled": self.github_backup.enabled,
            "token_set": bool(self.github_backup.token),
            "repo": self.github_backup.repo,
            "branch": self.github_backup.branch,
            "ok_count": 0,
            "error_count": 0,
            "last_ok_at": None,
            "last_error_at": None,
            "last_error": self.github_backup.last_error,
        }

        # Restore last weekly report from persisted state (dashboard shows it
        # even if this instance was started after the report was sent)
        reports = self.state.state.get("weekly_reports", [])
        if reports:
            last = reports[-1]
            if last.get("report"):
                self.last_weekly_report = last

    async def _send_startup_message(self):
        try:
            symbols = self.config.get("symbols", [])
            names = "".join(
                f"• {self.notifier.symbol_display_name(s)} (<code>{s}</code>)\n"
                for s in symbols
            )
            tfs = ", ".join(self.config.get("timeframes", []))
            status = market_status(self.config)
            session_text = {
                "regular": "🟢 Açık (normal seans)",
                "pre_market": "🌅 Pre-market",
                "after_hours": "🌙 After-hours",
                "closed": "🔴 Kapalı",
            }.get(status["session"], "🔴 Kapalı")
            now_tr = status["now_tr"].strftime("%Y-%m-%d %H:%M")
            sc_int = self._smallcap_scan_interval() // 60
            follow_line = f"📊 <b>Takip:</b>\n{names}" if names else ""
            sc_line = f"🔎 <b>Mid-cap tarayıcı:</b> her {sc_int} dk (otomatik keşif)\n" if self.smallcap_scanner else ""
            msg = (
                f"🤖 <b>NASDAQ Sinyal Botu Başlatıldı</b>\n"
                f"{'=' * 30}\n"
                f"{follow_line}"
                f"{sc_line}"
                f"⏱ <b>Periyotlar:</b> {tfs}\n"
                f"🔄 <b>Tarama:</b> her {self.scan_interval}s\n"
                f"🏙 <b>Piyasa:</b> {session_text}\n"
                f"🕐 {now_tr} (Türkiye saati)\n\n"
                f"Tarama başlıyor... 🔎"
            )
            await self.notifier.send_text(msg)
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

    async def _scan_once(self) -> int:
        """Run a single scan cycle, returns number of signals sent"""
        logger.info("=== Scanning market ===")

        price_data = await self.price_fetcher.fetch_all()

        news_data = {}
        try:
            news_data = await self.news_fetcher.fetch_all()
        except Exception as e:
            logger.error(f"News fetch failed: {e}")

        analysis_input = {}
        for symbol, tfs in price_data.items():
            analysis_input[symbol] = {}
            for tf, pd_obj in tfs.items():
                analysis_input[symbol][tf] = pd_obj.data

        snapshots = self.analyzer.analyze_all(analysis_input)
        self.last_snapshots = snapshots

        news_aggregates = {}
        for symbol in self.config.get("symbols", []):
            news_aggregates[symbol] = self.news_fetcher.get_aggregate_sentiment(symbol)
        # Ana symbols listesi boş olduğunda mid-cap zenginleştirmesinde çekilen
        # haberleri silme; yalnızca gerçek veri varken güncelle.
        if news_aggregates:
            self.last_news = news_aggregates

        signals = self.signal_engine.evaluate_all(snapshots, news_aggregates)
        self.last_signals = signals

        sent = 0
        for signal in signals:
            if self.state.is_signal_duplicate(signal.signal_id):
                continue
            if await self.notifier.send_signal(signal):
                self.state.record_signal(signal)
                sent += 1
                self._backup_async(
                    f"signals/{signal.symbol}/{signal.timestamp.isoformat()[:19].replace(':', '').replace('T', 'T')}.json",
                    {
                        "type": "signal",
                        "signal": {
                            "symbol": signal.symbol,
                            "action": signal.action,
                            "direction": signal.direction,
                            "strength": round(float(signal.strength), 2),
                            "price": signal.price,
                            "timeframe": signal.timeframe,
                            "id": signal.signal_id,
                            "reasons": signal.reasons,
                        },
                    },
                )

        self.state.set_last_scan()
        self.last_scan_at = datetime.utcnow()
        self.scan_count += 1

        logger.info(f"=== Scan complete: {len(signals)} signals, {sent} sent ===")
        return sent

    # ------------------------------------------------------------------
    # Weekly / daily reports (AI + screener)
    # ------------------------------------------------------------------

    def _weekly_report_key(self) -> str:
        iso = datetime.utcnow().isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def _daily_report_key(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def _should_run_weekly(self) -> bool:
        """Run weekly report if scheduled day/time reached and not sent this week"""
        sched = self.config.get("schedule", {}).get("weekly_report", {})
        key = self._weekly_report_key()
        if self.state.is_report_sent(key):
            return False
        now = now_turkey()
        day = int(sched.get("day_of_week", 6))
        hour = int(sched.get("hour_tr", 20))
        minute = int(sched.get("minute_tr", 0))
        return now.weekday() == day and (now.hour, now.minute) >= (hour, minute)

    def _should_run_daily(self) -> bool:
        """Run daily brief if scheduled window reached and not sent today"""
        sched = self.config.get("schedule", {}).get("daily_brief", {})
        if not sched.get("enabled", True):
            return False
        key = self._daily_report_key()
        if self.state.is_report_sent(key):
            return False
        now = now_turkey()
        if now.weekday() not in [int(d) for d in sched.get("days", [0, 1, 2, 3, 4])]:
            return False
        hour = int(sched.get("hour_tr", 15))
        minute = int(sched.get("minute_tr", 30))
        return (now.hour, now.minute) >= (hour, minute)

    async def run_weekly_report(self) -> bool:
        """Screen universe, generate AI report, send to Telegram, persist state"""
        key = self._weekly_report_key()
        logger.info(f"=== Generating weekly report ({key}) ===")
        try:
            candidates, index_stats = await self.screener.screen()
            top = candidates[: self.config.get("screener", {}).get("top_n", 12)]
            report = await self.ai_analyst.generate_weekly(top, index_stats)

            self.last_weekly_report = {
                "key": key,
                "generated_at": datetime.utcnow().isoformat(),
                "index": index_stats,
                "candidates": [c.to_dict() for c in top],
                "report": report,
            }

            if await self.notifier.send_report(report):
                self.state.record_report(
                    key,
                    {
                        "key": key,
                        "generated_at": self.last_weekly_report["generated_at"],
                        "candidates": [c.to_dict() for c in top],
                        "report": report,
                    },
                )
                # Haftalık çıkış adaylarını da prediction tracker'a ekle — böylece
                # "haftalık tutturma oranı" dashboard'da görülebilir.
                try:
                    self.state.update_prediction_tracker(
                        [c.to_dict() for c in top], source="weekly"
                    )
                except Exception as e:
                    logger.error(f"Weekly prediction tracker update failed: {e}")
                logger.info(f"✅ Weekly report sent ({key})")
                return True
        except Exception as e:
            logger.error(f"Weekly report failed: {e}")
            logger.exception(e)
        return False

    async def run_daily_brief(self) -> bool:
        """Screen today's triggers, generate AI brief, send to Telegram"""
        key = self._daily_report_key()
        logger.info(f"=== Generating daily brief ({key}) ===")
        try:
            candidates, index_stats = await self.screener.screen_daily()
            report = await self.ai_analyst.generate_daily(candidates, index_stats)

            self.last_daily_brief = {
                "key": key,
                "generated_at": datetime.utcnow().isoformat(),
                "index": index_stats,
                "candidates": [c.to_dict() for c in candidates[:8]],
                "report": report,
            }

            if await self.notifier.send_report(report):
                self.state.record_report(key)
                logger.info(f"✅ Daily brief sent ({key})")
                return True
        except Exception as e:
            logger.error(f"Daily brief failed: {e}")
            logger.exception(e)
        return False

    async def _maybe_run_reports(self):
        """Run scheduled reports if their window has arrived (called each loop)"""
        try:
            if self._should_run_weekly():
                await self.run_weekly_report()
            if self._should_run_daily():
                await self.run_daily_brief()
        except Exception as e:
            logger.error(f"Report scheduling check failed: {e}")

    # ------------------------------------------------------------------
    # Small-cap scanner (auto-discovered small caps, 15-min scans)
    # ------------------------------------------------------------------

    def _smallcap_scan_interval(self) -> int:
        open_interval = int(self.config.get("smallcap", {}).get("scan_interval_minutes", 15)) * 60
        closed_interval = int(self.config.get("smallcap", {}).get("closed_market_scan_interval_minutes", 60)) * 60
        try:
            status = market_status(self.config)
            if status.get("open", False):
                return open_interval
            return max(closed_interval, open_interval)
        except Exception:
            return open_interval

    def _should_scan_smallcap(self) -> bool:
        """Run small-cap scan every scan_interval_minutes."""
        if not self.smallcap_scanner or not self.smallcap_enabled():
            return False
        now = datetime.utcnow()
        if self.smallcap_last_scan is None:
            return True
        return (now - self.smallcap_last_scan).total_seconds() >= self._smallcap_scan_interval()

    def smallcap_enabled(self) -> bool:
        return bool(self.config.get("smallcap", {}).get("enabled", True))

    async def _run_smallcap_setup_report(self, candidates, universe_size):
        """Send the setup report once per day. Also records each sent candidate
        into the dashboard signal history so Telegram stocks appear in
        'Son Sinyaller' / 'Sinyal Geçmişi'."""
        try:
            date_key = datetime.utcnow().strftime("%Y-%m-%d")
            if self.state.is_smallcap_setup_sent(date_key):
                return
            report_text = self.smallcap_scanner.build_setup_report(candidates, universe_size)
            if report_text and await self.notifier.send_report(report_text):
                self.state.record_smallcap_setup_sent(date_key)
                for c in candidates:
                    self._record_smallcap_signal(c, action="ADAY")
                logger.info("✅ Small-cap setup report sent")
        except Exception as e:
            logger.error(f"Small-cap setup report failed: {e}")

    async def _run_smallcap_predictions_report(self, candidates, universe_size):
        """Send the 'kırılım öngörüsü' report once per day at market close.
        Runs when the market is closed to give next-session breakout forecast."""
        if not self.config.get("smallcap", {}).get("anticipation", {}).get("enabled", True):
            return
        try:
            date_key = datetime.utcnow().strftime("%Y-%m-%d")
            if self.state.is_smallcap_predictions_sent(date_key):
                return
            report_text = self.smallcap_scanner.build_predictions_report(candidates, universe_size)
            if report_text and await self.notifier.send_report(report_text):
                self.state.record_smallcap_predictions_sent(date_key)
                logger.info("✅ Small-cap tomorrow-predictions report sent")
                self._backup_async(
                    f"reports/predictions_{date_key}.json",
                    {
                        "type": "predictions_report",
                        "date": date_key,
                        "universe_size": universe_size,
                        "candidates": [c.to_dict() for c in candidates],
                    },
                )
        except Exception as e:
            logger.error(f"Small-cap predictions report failed: {e}")

    async def _run_smallcap_premarket_report(self, candidates, universe_size):
        """Send 'BUGÜN İZLE' pre-market report once per day before the open."""
        if not self.config.get("smallcap", {}).get("premarket_report", {}).get("enabled", True):
            return
        try:
            date_key = datetime.utcnow().strftime("%Y-%m-%d")
            if self.state.is_smallcap_premarket_sent(date_key):
                return
            report_text = self.smallcap_scanner.build_premarket_report(candidates, universe_size)
            if report_text and await self.notifier.send_report(report_text):
                self.state.record_smallcap_premarket_sent(date_key)
                logger.info("✅ Small-cap 'BUGÜN İZLE' pre-market report sent")
                self._backup_async(
                    f"reports/premarket_{date_key}.json",
                    {
                        "type": "premarket_report",
                        "date": date_key,
                        "universe_size": universe_size,
                        "candidates": [c.to_dict() for c in candidates],
                    },
                )
        except Exception as e:
            logger.error(f"Small-cap pre-market report failed: {e}")

    async def _run_smallcap_alert(self, cand):
        """Send a single breakout alarm, deduplicated per symbol per day."""
        try:
            date_key = datetime.utcnow().strftime("%Y-%m-%d")
            if self.state.is_smallcap_alert_sent(cand["symbol"], date_key):
                return False
            msg = self.smallcap_scanner.build_trigger_message(
                self._candidate_from_dict(cand)
            )
            if await self.notifier.send_smallcap_alert(msg):
                self.state.record_smallcap_alert_sent(cand["symbol"], date_key)
                c = self._candidate_from_dict(cand)
                self._record_smallcap_signal(c, action="BREAKOUT")
                # Telegram'a atılan kırılım alarmını da fiyat takibine al
                # (dashboard'daki 'Telegram Sinyal Takibi' paneli için: limit/hedef/stop
                # ile birlikte, hedef tutunca ✓ / stop olunca ✗ gösterilir).
                try:
                    self.state.update_prediction_tracker(
                        [c.with_plans(self.smallcap_scanner)], source="telegram"
                    )
                except Exception as e:
                    logger.warning(f"Telegram prediction track failed {cand['symbol']}: {e}")
                return True
        except Exception as e:
            logger.error(f"Small-cap alert failed: {e}")
        return False

    def _record_smallcap_signal(self, c, action: str):
        """Record a Telegram-sent small-cap stock into the dashboard signal
        history + 'Son Sinyaller'. Falls back silently on any error."""
        try:
            price = float(getattr(c, "price", 0) or 0)
            if price <= 0:
                return
            strength = float(getattr(c, "setup_score", 0) or 0)
            ts = datetime.utcnow()
            entry = {
                "id": hashlib.md5(
                    f"{c.symbol}_{action}_{ts.strftime('%Y%m%d%H%M')}".encode()
                ).hexdigest()[:12],
                "symbol": c.symbol,
                "action": action,
                "direction": "LONG",
                "strength": strength,
                "price": price,
                "timestamp": ts.isoformat(),
            }
            self.state.state.setdefault("signal_history", []).append(entry)
            if self.state.state.get("stats", {}).get("total_signals") is not None:
                self.state.state["stats"]["total_signals"] += 1
            self.state.save()
            logger.info(f"📈 Signal kaydı: {c.symbol} ({action}) @ {price}")
        except Exception as e:
            logger.warning(f"Small-cap signal record failed {getattr(c, 'symbol', '?')}: {e}")

    def _backup_async(self, path: str, payload: Dict[str, Any]):
        """Fire-and-forget upload to the GitHub backup repo (never blocks the loop)."""
        try:
            task = asyncio.create_task(self.github_backup.upload_json(path, payload))
            task.add_done_callback(lambda t: self._backup_done(t, path))
        except Exception as e:
            logger.warning(f"GitHub backup schedule failed {path}: {e}")

    async def _restore_state_from_backup(self):
        """Restore state from GitHub backup if local state is empty or missing."""
        try:
            if not self.github_backup or not self.github_backup.enabled:
                return

            scan_hist = self.state.state.get("scan_history", [])
            sig_hist = self.state.state.get("signal_history", [])

            # Skip restore only if local state already has active history
            if len(scan_hist) > 0 or len(sig_hist) > 0:
                logger.info(f"State exists locally with {len(scan_hist)} scans & {len(sig_hist)} signals — restoring memory pointers")
                # Re-hydrate candidates pointer from the latest scan history entry
                if scan_hist:
                    last_scan = scan_hist[-1]
                    self.smallcap_candidates = last_scan.get("candidates", [])
                    self.smallcap_universe_size = last_scan.get("universe_size", 0)
                    if last_scan.get("time"):
                        try:
                            self.smallcap_last_scan = datetime.fromisoformat(last_scan["time"])
                        except Exception:
                            pass
                return

            logger.info("Local state is empty — downloading latest state from GitHub...")
            raw = await self.github_backup.download_json("state/bot_state.json")
            if not raw:
                # Secondary fallback: try raw repository root state
                raw = await self.github_backup.download_json("data/bot_state.json")

            if raw:
                self.state.load_raw(raw)
                scan_hist = self.state.state.get("scan_history", [])
                if scan_hist:
                    last_scan = scan_hist[-1]
                    self.smallcap_candidates = last_scan.get("candidates", [])
                    self.smallcap_universe_size = last_scan.get("universe_size", 0)
                    if last_scan.get("time"):
                        try:
                            self.smallcap_last_scan = datetime.fromisoformat(last_scan["time"])
                        except Exception:
                            pass

                reports = self.state.state.get("weekly_reports", [])
                if reports:
                    last = reports[-1]
                    if last.get("report"):
                        self.last_weekly_report = last

                logger.info(
                    "State successfully restored from GitHub backup "
                    f"({len(scan_hist)} scans, {len(self.state.state.get('signal_history', []))} signals)"
                )
            else:
                logger.info("No GitHub state backup found yet — starting fresh")
        except Exception as e:
            logger.warning(f"State restore failed: {e}")

    def _backup_state_async(self):
        """Fire-and-forget backup of the whole bot_state.json to the archive branch."""
        try:
            if not self.github_backup or not self.github_backup.enabled:
                return
            payload = dict(self.state.state)
            self._backup_async("state/bot_state.json", payload)
        except Exception as e:
            logger.warning(f"State backup schedule failed: {e}")

    def _backup_done(self, task: asyncio.Task, path: str):
        """Update dashboard-visible backup status from the background task."""
        try:
            ok = task.result() is True
            if ok:
                self.backup_status["ok_count"] += 1
                self.backup_status["last_ok_at"] = datetime.utcnow().isoformat()
                self.backup_status["last_error"] = None
            else:
                self.backup_status["error_count"] += 1
                self.backup_status["last_error_at"] = datetime.utcnow().isoformat()
                self.backup_status["last_error"] = self.github_backup.last_error
            self.backup_status["last_path"] = path
        except Exception as e:
            self.backup_status["error_count"] += 1
            self.backup_status["last_error_at"] = datetime.utcnow().isoformat()
            self.backup_status["last_error"] = str(e)

    def _persist_smallcap_scan(self, closed: bool = False):
        """Save the latest mid-cap scan snapshot (top candidates) to persisted state
        so the dashboard history survives restarts."""
        try:
            top = self.smallcap_candidates[: self.config.get("smallcap", {}).get("top_n_report", 10)]
            snapshot = {
                "time": datetime.utcnow().isoformat(),
                "universe_size": self.smallcap_universe_size,
                "closed": bool(closed),
                "candidates": [
                    {
                        "symbol": c.get("symbol", ""),
                        "name": c.get("name", ""),
                        "price": c.get("price", 0),
                        "change_pct": c.get("change_pct", 0),
                        "setup_score": c.get("setup_score", 0),
                        "setup_type": c.get("setup_type", "watch"),
                        "anticipation_score": c.get("anticipation_score", 0),
                        "expect_horizon": c.get("expect_horizon", "birikim"),
                        "squeeze_days": int(c.get("squeeze_days", 0)),
                        "trigger_type": c.get("trigger_type"),
                        "news_score": c.get("news_score", 0),
                    }
                    for c in top
                ],
            }
            self.state.record_scan_history(snapshot)
            # Live backtest: recommend edilen adayların limit/hedef/stop takibi
            try:
                self.state.update_prediction_tracker(self.smallcap_candidates)
            except Exception as e:
                logger.error(f"Prediction tracker update failed: {e}")
            # GitHub backup: her tarama JSON olarak archive branch'ına yazılır
            day = snapshot["time"][:10]
            stamp = snapshot["time"][:19].replace(":", "").replace("T", "T")
            self._backup_async(
                f"scans/{day}/{stamp}.json",
                {"type": "scan", "closed": bool(closed), **snapshot},
            )
            # State'in tamamını da arşivle (prediction tracker dahil) — redeploy'da
            # geçici disk kaybolsa bile bot_state.json archive'den geri yüklenir.
            self._backup_state_async()
        except Exception as e:
            logger.error(f"Scan history persist failed: {e}")

    def _candidate_from_dict(self, d: Dict[str, Any]):
        """Rebuild a SmallCapCandidate from its dict (for message building)."""
        try:
            from src.analysis.smallcap_scanner import SmallCapCandidate
            return SmallCapCandidate(
                symbol=d["symbol"],
                name=d.get("name", d["symbol"]),
                price=d.get("price", 0),
                change_pct=d.get("change_pct", 0),
                market_cap=d.get("market_cap", 0),
                setup_score=d.get("setup_score", 0),
                setup_type=d.get("setup_type", "watch"),
                reasons=d.get("reasons", []),
                rsi_14=d.get("rsi_14", 50),
                bb_width_percentile=d.get("bb_width_percentile", 50),
                dist_52w_high_pct=d.get("dist_52w_high_pct", 0),
                vol_ratio=d.get("vol_ratio", 1.0),
                rs_4w=d.get("rs_4w", 0),
                donchian_upper=d.get("donchian_upper", 0),
                anticipation_score=d.get("anticipation_score", 0),
                dist_to_resistance_pct=d.get("dist_to_resistance_pct", 0),
                bbw_slope_pct=d.get("bbw_slope_pct", 0),
                squeeze_days=int(d.get("squeeze_days", 0)),
                atr_pct=float(d.get("atr_pct", 0) or 0),
                atr_contraction_pct=d.get("atr_contraction_pct", 0),
                expect_horizon=d.get("expect_horizon", "birikim"),
                trigger_score=d.get("trigger_score", 0),
                trigger_type=d.get("trigger_type"),
                trigger_reasons=d.get("trigger_reasons", []),
                news_score=d.get("news_score", 0),
                news_headline=d.get("news_headline", ""),
                news_source=d.get("news_source", ""),
            )
        except Exception as e:
            logger.error(f"Candidate rebuild failed: {e}")
            return None

    async def _enrich_candidates_with_news(self, candidates) -> List:
        """Fetch news for top mid-cap candidates and attach sentiment scores.
        Returns a new list of SmallCapCandidate objects with news fields set.
        Falls back silently — news is an optional enrichment. Bounded by a short
        timeout so slow/blocked news APIs never stall the scan cycle."""
        if not candidates or not self.config.get("news", {}).get("enabled", True):
            return candidates

        news_timeout = float(self.config.get("news", {}).get("enrich_timeout_seconds", 10))
        watch_n = int(self.config.get("smallcap", {}).get("watchlist_size", 25))
        top = candidates[:watch_n]
        tickers = [c.symbol for c in top if c.symbol]
        if not tickers:
            return candidates

        try:
            news = await asyncio.wait_for(
                self.news_fetcher.fetch_for_tickers(tickers),
                timeout=news_timeout,
            )
            for c in top:
                agg = self.news_fetcher.get_aggregate_sentiment(c.symbol)
                if agg and agg.get("article_count", 0) > 0:
                    c.news_score = float(agg.get("score", 0.0))
                    articles = news.get(c.symbol, [])
                    if articles:
                        c.news_headline = articles[0].title
                        c.news_source = articles[0].source
                    # Dashboard "Haber Akışı" bölümüne de bağla
                    self.last_news[c.symbol] = agg
        except asyncio.TimeoutError:
            logger.warning(f"News enrichment timed out after {news_timeout}s — skipped (news optional)")
        except Exception as e:
            logger.error(f"News enrichment failed: {e}")

        return candidates

    async def _scan_smallcap_once(self):
        """One full small-cap cycle: setup ranking + intraday trigger scan."""
        logger.info("=== Scanning small-cap universe ===")
        try:
            force_universe = False
            candidates, universe = await self.smallcap_scanner.screen_setups(force_universe=force_universe)
            self.smallcap_universe_size = len(universe)

            candidates = await self._enrich_candidates_with_news(candidates)

            top_dicts = [c.with_plans(self.smallcap_scanner) for c in candidates]
            self.smallcap_candidates = top_dicts

            # Default: no trigger scan pre-market/after-hours
            status = market_status(self.config)
            market_open = status.get("open", False)
            trade_hours_only = self.config.get("smallcap", {}).get("trade_hours_only", True)

            if trade_hours_only and not market_open:
                session = status.get("session", "closed")
                is_premarket_window = session == "pre_market" and premarket_report_due(self.config, status.get("now_et"))
                if is_premarket_window:
                    logger.info("Small-cap: pre-market penceresi, 'BUGÜN İZLE' raporu üretiliyor")
                    await self._run_smallcap_premarket_report(candidates, len(universe))
                else:
                    logger.info(f"Small-cap: piyasa kapalı ({session}), öngörü raporu üretiliyor")
                    await self._run_smallcap_predictions_report(candidates, len(universe))
                self._persist_smallcap_scan(closed=True)
                self.smallcap_last_scan = datetime.utcnow()
                return

            # Trigger scan on watchlist (intraday 15m)
            watch, triggered = await self.smallcap_scanner.scan_triggers(candidates, universe)
            # Update candidates with trigger info
            updated = {}
            for c in watch:
                updated[c.symbol] = c
            final_list = []
            for cd in (c.with_plans(self.smallcap_scanner) for c in candidates):
                if cd["symbol"] in updated:
                    rc = updated[cd["symbol"]]
                    cd.update(rc.to_dict())
                final_list.append(cd)
            self.smallcap_candidates = final_list

            # Send setup report (throttled)
            await self._run_smallcap_setup_report(candidates, len(universe))

            # Send breakout alarms (dedup per day)
            for cand in watch:
                if cand.trigger_type == "breakout" and cand.trigger_score >= self.config.get("smallcap", {}).get(
                        "min_trigger_score", 60):
                    cand_dict = cand.to_dict()
                    await self._run_smallcap_alert(cand_dict)

            self.smallcap_last_scan = datetime.utcnow()
            self._persist_smallcap_scan(closed=False)
            await self._run_chart_analyses(candidates)
            logger.info(f"=== Small-cap scan complete: {len(candidates)} aday, {len(triggered)} breakout ===")
        except Exception as e:
            logger.error(f"Small-cap scan error: {e}")
            logger.exception(e)

    def _maybe_scan_smallcap(self) -> bool:
        """Non-async check - the caller runs the scan task. Returns True if due."""
        return self._should_scan_smallcap()

    async def _run_initial_reports(self):
        """
        On startup: if the market is closed and a report hasn't been generated
        yet this week/today, generate it now (so weekend deployments still deliver).
        """
        try:
            await asyncio.sleep(5)  # let the first scan settle
            status = market_status(self.config)
            if not status["open"]:
                if self.config.get("schedule", {}).get("weekly_report", {}).get("run_if_closed_on_startup", True):
                    if not self.state.is_report_sent(self._weekly_report_key()):
                        await self.run_weekly_report()
                if not self.state.is_report_sent(self._daily_report_key()) and self._should_run_daily():
                    await self.run_daily_brief()
        except Exception as e:
            logger.error(f"Initial report run failed: {e}")

    def _scan_timeout(self) -> float:
        """Max seconds a whole scan cycle may take before being force-cancelled.
        Prevents a stuck network call from leaving the dashboard empty forever."""
        return float(self.config.get("scanner", {}).get("scan_timeout", 180))

    async def run(self):
        """Main run loop"""
        self.is_running = True
        logger.info(f"🤖 Bot started with interval {self.scan_interval}s")
        await self._restore_state_from_backup()
        await self._send_startup_message()
        asyncio.create_task(self._run_initial_reports())
        asyncio.create_task(self._smallcap_loop())
        asyncio.create_task(self._backtest_loop())

        while self.is_running:
            try:
                timeout = self._scan_timeout()
                await asyncio.wait_for(
                    self._scan_cycle(),
                    timeout=timeout,
                )
                self.health_ok = True
            except asyncio.TimeoutError:
                logger.error("Scan cycle timed out after %ss", self._scan_timeout())
                self.health_ok = False
                self.error_count += 1
            except Exception as e:
                logger.error(f"Scan cycle error: {e}")
                logger.exception(e)
                self.health_ok = False
                self.error_count += 1
            finally:
                await asyncio.sleep(self.scan_interval)

    async def _smallcap_loop(self):
        """Dedicated small-cap scanner loop, decoupled from the main 60s cycle.
        The mid-cap scan can legitimately take minutes (250 symbols), so it runs
        in its own task with a generous timeout and never blocks the dashboard."""
        smallcap_timeout = float(self.config.get("smallcap", {}).get("scan_timeout_seconds", 1500))
        while self.is_running:
            try:
                if self._maybe_scan_smallcap():
                    await asyncio.wait_for(
                        self._scan_smallcap_once(),
                        timeout=smallcap_timeout,
                    )
            except asyncio.TimeoutError:
                logger.error(f"Small-cap scan exceeded {smallcap_timeout}s — will retry")
                self.error_count += 1
            except Exception as e:
                logger.error(f"Small-cap loop error: {e}")
                logger.exception(e)
                self.error_count += 1
            finally:
                await asyncio.sleep(self._smallcap_scan_interval() + 5)

    async def _run_chart_analyses(self, candidates):
        """Chart reading for the top candidates.
        Always runs the rule-based 'Mum Mantığı + Candle Blending' analysis
        (price_action_analysis). If an LLM key is set, the candlestick image is
        also sent to the vision model with the same system rules (budget-capped).
        Results go to last_chart_analyses for the dashboard. Never blocks the
        scan cycle — a short timeout applies on LLM calls."""
        try:
            cc = self.config.get("ai_report", {}).get("chart_analysis", {}) or {}
            if not cc.get("enabled", True):
                return
            top_n = int(cc.get("top_n", 3))
            top = candidates[:top_n]
            if not top:
                return

            from src.analysis.chart_builder import build_candlestick_chart
            from src.analysis.pattern import price_action_analysis

            has_llm = bool(self.ai_analyst) and self.ai_analyst._has_llm()

            # Günlük bütçe yalnızca LLM (görsel) çağrıları için geçerli
            if has_llm:
                daily_budget = int(cc.get("daily_budget_calls", 6))
                today = datetime.utcnow().strftime("%Y-%m-%d")
                if self._chart_analysis_day != today:
                    self._chart_analysis_day = today
                    self._chart_analysis_budget = daily_budget

            symbols = [c.symbol for c in top]
            fetched = await self.smallcap_scanner._fetch_timeframes(symbols, ["1d"])
            for cand in top:
                pd_obj = fetched.get(cand.symbol, {}).get("1d")
                if pd_obj is None or pd_obj.data is None:
                    continue

                # 1) Kural tabanlı analiz her zaman çalışır (AI'sız)
                rule = price_action_analysis(pd_obj.data)
                chart = build_candlestick_chart(pd_obj.data, cand.symbol, "1d")
                context = {
                    "symbol": cand.symbol,
                    "price": cand.price,
                    "change_pct": cand.change_pct,
                    "setup_score": cand.setup_score,
                    "setup_type": cand.setup_type,
                    "rsi_14": round(cand.rsi_14, 1),
                    "volume_ratio": round(cand.vol_ratio, 2),
                    "resistance_pivot": cand.resistance_pivot,
                    "support_pivot": cand.support_pivot,
                    "candle_patterns": cand.candle_patterns,
                    "expect_horizon": cand.expect_horizon,
                    "price_action": {
                        "bias": rule["bias"],
                        "direction": rule["direction"],
                        "score": rule["score"],
                    },
                }

                provider = "rule_based"
                parts = []
                if rule["verdict"]:
                    lines = [f"🧭 {rule['verdict']}", f"📊 Yön beklentisi: {rule['direction'].upper()}"]
                    lines += [f"• {s}" for s in rule["steps"]]
                    parts.append("\n".join(lines))

                # 2) LLM yoksa veya bütçe bittiyse sadece kural tabanlı sonuç kaydedilir
                if has_llm and chart and self._chart_analysis_budget > 0:
                    result = await asyncio.wait_for(
                        self.ai_analyst.analyze_chart(cand.symbol, chart, context),
                        timeout=45,
                    )
                    self._chart_analysis_budget -= 1
                    if result and result.get("comment"):
                        provider = self.ai_analyst.provider
                        parts.append(f"🤖 Görsel analiz:\n{result['comment']}")

                if parts:
                    self.last_chart_analyses[cand.symbol] = {
                        "symbol": cand.symbol,
                        "provider": provider,
                        "rule": rule,
                        "analyzed_at": datetime.utcnow().isoformat(),
                        "comment": "\n\n".join(parts),
                    }
        except Exception as e:
            logger.warning(f"Chart analysis skipped: {e}")

    async def _backtest_loop(self):
        """Periodic historical backtest of the breakout strategy (real P&L).
        Runs on the closed market (evening TR / overnight) once a day so it never
        competes with live scans for yfinance quota. Results are stored in state
        and shown on the dashboard as "Sistem gerçekte ne kazandırıyor?"."""
        interval = float(self.config.get("backtest", {}).get("interval_seconds", 21600))
        bt_cfg = self.config.get("backtest", {})
        while self.is_running:
            try:
                await self._run_backtest_once()
            except Exception as e:
                logger.error(f"Backtest loop error: {e}")
            await asyncio.sleep(interval)

    async def _run_backtest_once(self):
        """Run backtest on the current mid-cap universe (daily data)."""
        from src.utils.timezone import now_turkey

        bt_cfg = self.config.get("backtest", {})
        if not bt_cfg.get("enabled", True):
            return
        status = market_status(self.config)
        # Only run while market closed (or if forced) to avoid quota contention
        if status.get("open", True) and not bt_cfg.get("run_during_hours", False):
            return

        now = now_turkey()
        logger.info(f"Backtest başlıyor ({now.strftime('%d.%m.%Y %H:%M')} TR)...")
        try:
            if not self.smallcap_scanner:
                return
            universe = await self.smallcap_scanner.universe_fetcher.fetch_universe(force=False)
            symbols = [u["symbol"] for u in universe[: bt_cfg.get("max_symbols", 60)]]
            if not symbols:
                return
            fetched = await self.smallcap_scanner._fetch_timeframes(symbols, ["1d"])
            data = {}
            for sym in symbols:
                pd_obj = fetched.get(sym, {}).get("1d")
                if pd_obj is not None and pd_obj.data is not None:
                    data[sym] = pd_obj.data
            summary = run_backtest_for_symbols(self.config, data)
            self.state.state["backtest_results"] = summary
            self.state.save()
            self._backup_state_async()
            logger.info(
                f"Backtest tamam: {summary.get('total_trades', 0)} trade, "
                f"win rate %{summary.get('win_rate_pct', 0)}, "
                f"ortalama getiri %{summary.get('avg_symbol_return_pct', 0)}"
            )
        except Exception as e:
            logger.error(f"Backtest run error: {e}")
            logger.exception(e)

    async def _scan_cycle(self):
        """One full scan iteration (guarded by asyncio.wait_for in run)"""
        await self._scan_once()
        await self._maybe_run_reports()

    async def stop(self):
        """Graceful shutdown"""
        self.is_running = False
        self.state.save()
        self._backup_state_async()
        await asyncio.sleep(0.5)
        await self.notifier.close()
        await self.news_fetcher.close()
        await self.ai_analyst.close()
        if self.smallcap_scanner:
            await self.smallcap_scanner.universe_fetcher.close()
        if self.github_backup:
            await self.github_backup.close()
        logger.info("Bot stopped gracefully")

    # ------------------------------------------------------------------
    # Dashboard helpers
    # ------------------------------------------------------------------

    def snapshot_to_dict(self, snap: IndicatorSnapshot) -> Dict[str, Any]:
        """Convert a snapshot to a JSON-serializable dict"""
        import math

        def clean(v):
            if v is None:
                return 0
            try:
                f = float(v)
                return 0 if math.isnan(f) or math.isinf(f) else f
            except (ValueError, TypeError):
                return 0

        return {
            "symbol": snap.symbol,
            "timeframe": snap.timeframe,
            "price": clean(snap.price),
            "change_pct": round(clean(snap.change_pct), 2),
            "composite_score": round(clean(snap.composite_score), 1),
            "trend_score": round(clean(snap.trend_score), 1),
            "momentum_score": round(clean(snap.momentum_score), 1),
            "volume_score": round(clean(snap.volume_score), 1),
            "breakout_score": round(clean(snap.breakout_score), 1),
            "rsi_14": round(clean(snap.rsi_14), 1),
            "volume_ratio": round(clean(snap.volume_ratio), 2),
            "atr_14": round(clean(snap.atr_14), 2),
            "is_breakout_up": snap.is_breakout_up,
            "is_breakout_down": snap.is_breakout_down,
            "is_volume_spike": snap.is_volume_spike,
            "is_golden_cross": snap.is_golden_cross,
            "is_death_cross": snap.is_death_cross,
            "is_vwap_reclaim": snap.is_vwap_reclaim,
        }

    def _ai_status(self) -> Dict[str, Any]:
        """AI integration status shown in the UI. Analysis is rule-based and
        works WITHOUT AI; the LLM only enriches text/vision reports."""
        ai_cfg = self.config.get("ai_report", {}) or {}
        provider = os.getenv("AI_PROVIDER", "").strip().lower() or ai_cfg.get("provider", "openai")
        gemini_key = bool(os.getenv("GEMINI_API_KEY", "").strip())
        openai_key = bool(os.getenv("OPENAI_API_KEY", "").strip())
        if provider == "gemini":
            connected = gemini_key
            label = "Gemini (ücretsiz)" if gemini_key else "Gemini (key eksik)"
        elif provider == "openai":
            connected = openai_key
            label = "OpenAI" if openai_key else "OpenAI (key eksik)"
        else:
            connected = False
            label = provider
        return {
            "provider": provider,
            "connected": bool(connected),
            "label": label,
            "mode": "ai" if connected else "rule_based",
            "note": (
                "🧠 AI destekli analiz aktif"
                if connected else
                "Analiz AI'sız çalışıyor — kural tabanlı (tamamen ücretsiz ve bağımsız)"
            ),
        }

    def get_dashboard_state(self) -> Dict[str, Any]:
        """Full state for the dashboard API"""
        symbols = self.config.get("symbols", [])
        timeframes = self.config.get("timeframes", [])

        market = {}
        for symbol in symbols:
            market[symbol] = {}
            tfs = self.last_snapshots.get(symbol, {})
            for tf in timeframes:
                snap = tfs.get(tf)
                if snap:
                    market[symbol][tf] = self.snapshot_to_dict(snap)
                else:
                    market[symbol][tf] = None

        signals = []
        for s in self.last_signals:
            signals.append({
                "id": s.signal_id,
                "symbol": s.symbol,
                "action": s.action,
                "direction": s.direction,
                "strength": round(s.strength, 1),
                "price": s.price,
                "timeframe": s.timeframe,
                "timestamp": s.timestamp.isoformat(),
                "reasons": s.reasons,
            })
        # Telegram'a gönderilen small-cap hisselerini de 'Son Sinyaller'e ekle
        # (setup raporu adayları + breakout alertleri). Sinyal motoru boşken
        # bile dashboard kullanıcının Telegram'da gördüğü hisseleri gösterir.
        smallcap_sigs = [
            s for s in self.state.state.get("signal_history", [])[-20:]
            if s.get("action") in ("ADAY", "BREAKOUT")
        ][::-1]
        signals = smallcap_sigs + signals

        mkt_status = market_status(self.config)

        budget = self.config.get("budget", {}) or {}
        budget_usd = 0.0
        rate = float(budget.get("usd_try_rate", 0) or 0)
        if rate > 0:
            budget_usd = float(budget.get("budget_try", 0) or 0) / rate

        smallcap_info = {
            "enabled": self.smallcap_scanner is not None,
            "universe_size": self.smallcap_universe_size,
            "last_scan_at": self.smallcap_last_scan.isoformat() if self.smallcap_last_scan else None,
            "scan_interval_minutes": self._smallcap_scan_interval() // 60,
            "candidates": self.smallcap_candidates[: self.config.get("smallcap", {}).get("top_n_report", 10)],
            "scan_history": self.state.get_scan_history(limit=50),
        }

        budget_info = {
            "budget_try": float(budget.get("budget_try", 0) or 0),
            "usd_try_rate": rate,
            "budget_usd": round(budget_usd, 2),
            "risk_per_trade_pct": float(budget.get("risk_per_trade_pct", 2.0) or 2.0),
            "max_trades": int(budget.get("max_trades", 3) or 3),
        }

        return {
            "status": {
                "running": self.is_running,
                "healthy": self.health_ok,
                "scan_count": self.scan_count,
                "error_count": self.error_count,
                "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
                "scan_interval_seconds": self.scan_interval,
                "market_open": mkt_status["open"],
                "market_session": mkt_status["session"],
                "time_ny": mkt_status["now_et"].strftime("%Y-%m-%d %H:%M %Z"),
                "time_tr": mkt_status["now_tr"].strftime("%Y-%m-%d %H:%M %Z"),
            },
            "config": {
                "symbols": symbols,
                "timeframes": timeframes,
                "thresholds": self.config.get("thresholds", {}),
            },
            "market": market,
            "signals": signals,
            "news": self.last_news,
            "stats": self.state.get_stats(),
            "history": self.state.state.get("signal_history", [])[-20:],
            "weekly_report": self.last_weekly_report,
            "daily_brief": self.last_daily_brief,
            "smallcap": smallcap_info,
            "budget": budget_info,
            "predictions": self.state.get_prediction_stats(),
            "prediction_details": self.state.get_prediction_details(source="weekly"),
            "daily_prediction_details": self.state.get_prediction_details(source="daily"),
            "telegram_prediction_details": self.state.get_prediction_details(source="telegram"),
            "weekly_tracking": self.state.get_prediction_stats(source="weekly"),
            "backtest": self.state.state.get("backtest_results"),
            "ai_status": self._ai_status(),
            "chart_analyses": self.last_chart_analyses,
            "backup": self.backup_status,
        }
