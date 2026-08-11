"""
Web App - Dashboard + Bot Host
Runs the bot in background and serves a live dashboard on Render.
Usage: uvicorn src.webapp:app --host 0.0.0.0 --port $PORT
"""
import os
import sys
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import inspect
import uvicorn

from src.utils.logger import get_logger
from src.utils.timezone import market_status
from src.bot import SignalBot, load_config

logger = get_logger("webapp")

# Global bot instance
bot: SignalBot = None
config = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start bot as background task on app startup"""
    global bot, config
    config = load_config()
    bot = SignalBot(config)

    task = asyncio.create_task(bot.run())
    logger.info("Bot background task started")

    yield

    logger.info("Shutting down bot...")
    await bot.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="NASDAQ Signal Bot", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "web", "templates")
STATIC_DIR = os.path.join(BASE_DIR, "web", "static")

os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def render_template(request: Request, name: str, context: dict):
    """Render a Jinja2 template with Starlette 0.x / 1.x compatibility."""
    try:
        sig = inspect.signature(templates.TemplateResponse)
        params = list(sig.parameters.keys())
        if params and params[0] == "request":
            return templates.TemplateResponse(request, name, context)
    except (TypeError, ValueError):
        pass
    return templates.TemplateResponse(name, {**context, "request": request})


def get_bot() -> Optional[SignalBot]:
    """Return the bot instance or None if not ready"""
    return bot


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return render_template(request, "dashboard.html", {"request": request})


@app.get("/health")
async def health():
    """Health check for uptime monitoring / Render"""
    b = bot
    status = "ok"
    code = 200
    if b is not None and not b.health_ok:
        status = "degraded"
        code = 503
    market = market_status(config) if config else market_status({})
    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "running": b.is_running if b else False,
            "last_scan": b.last_scan_at.isoformat() if b and b.last_scan_at else None,
            "scan_count": b.scan_count if b else 0,
            "market_open": market["open"],
            "market_session": market["session"],
            "time_ny": market["now_et"].strftime("%Y-%m-%d %H:%M %Z"),
            "time_tr": market["now_tr"].strftime("%Y-%m-%d %H:%M %Z"),
            "time": datetime.utcnow().isoformat(),
        },
    )


@app.get("/api/status")
async def api_status():
    b = get_bot()
    if b is None:
        return {"running": False, "healthy": False, "scan_count": 0, "error_count": 0,
                "last_scan_at": None, "scan_interval_seconds": 60,
                "symbols": [], "timeframes": []}
    market = market_status(b.config)
    return {
        "running": b.is_running,
        "healthy": b.health_ok,
        "scan_count": b.scan_count,
        "error_count": b.error_count,
        "last_scan_at": b.last_scan_at.isoformat() if b.last_scan_at else None,
        "scan_interval_seconds": b.scan_interval,
        "symbols": b.config.get("symbols", []),
        "timeframes": b.config.get("timeframes", []),
        "backup": b.backup_status,
        "ai_status": b._ai_status(),
        "market_open": market["open"],
        "market_session": market["session"],
        "time_ny": market["now_et"].strftime("%Y-%m-%d %H:%M %Z"),
        "time_tr": market["now_tr"].strftime("%Y-%m-%d %H:%M %Z"),
    }


@app.get("/api/market")
async def api_market():
    b = get_bot()
    if b is None:
        return {}
    market = {}
    for symbol, tfs in b.last_snapshots.items():
        market[symbol] = {}
        for tf, snap in tfs.items():
            market[symbol][tf] = b.snapshot_to_dict(snap)
    return market


@app.get("/api/signals")
async def api_signals(limit: int = 20):
    b = get_bot()
    if b is None:
        return []
    history = b.state.state.get("signal_history", [])
    recent = history[-limit:][::-1] if history else []
    return recent


@app.get("/api/news")
async def api_news():
    b = get_bot()
    if b is None:
        return {}
    return b.last_news


@app.get("/api/dashboard")
async def api_dashboard():
    b = get_bot()
    if b is None:
        return {"status": {"running": False, "healthy": False, "scan_count": 0,
                           "error_count": 0, "last_scan_at": None, "scan_interval_seconds": 60},
                "config": {"symbols": [], "timeframes": [], "thresholds": {}},
                "market": {}, "signals": [], "news": {}, "stats": {}, "history": []}
    return b.get_dashboard_state()


@app.get("/api/weekly-report")
async def api_weekly_report():
    b = get_bot()
    if b is None:
        return {"report": None, "candidates": [], "index": {}}
    wr = b.last_weekly_report
    if not wr:
        return {"report": None, "candidates": [], "index": {}}
    return {
        "key": wr.get("key"),
        "generated_at": wr.get("generated_at"),
        "report": wr.get("report"),
        "index": wr.get("index"),
        "candidates": wr.get("candidates"),
    }


@app.get("/favicon.ico")
async def favicon():
    favicon_path = os.path.join(STATIC_DIR, "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    return JSONResponse(status_code=204, content=None)


# Mount static files at /static (directory always exists due to makedirs above)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
