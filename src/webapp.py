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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from src.utils.logger import get_logger
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


def get_bot() -> SignalBot:
    if bot is None:
        raise RuntimeError("Bot not initialized")
    return bot


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/health")
async def health():
    """Health check for uptime monitoring / Render"""
    b = bot
    status = "ok"
    code = 200
    if b is not None and not b.health_ok:
        status = "degraded"
        code = 503
    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "running": b.is_running if b else False,
            "last_scan": b.last_scan_at.isoformat() if b and b.last_scan_at else None,
            "scan_count": b.scan_count if b else 0,
            "time": datetime.utcnow().isoformat(),
        },
    )


@app.get("/api/status")
async def api_status():
    b = get_bot()
    return {
        "running": b.is_running,
        "healthy": b.health_ok,
        "scan_count": b.scan_count,
        "error_count": b.error_count,
        "last_scan_at": b.last_scan_at.isoformat() if b.last_scan_at else None,
        "scan_interval_seconds": b.scan_interval,
        "symbols": b.config.get("symbols", []),
        "timeframes": b.config.get("timeframes", []),
    }


@app.get("/api/market")
async def api_market():
    b = get_bot()
    market = {}
    for symbol, tfs in b.last_snapshots.items():
        market[symbol] = {}
        for tf, snap in tfs.items():
            market[symbol][tf] = b.snapshot_to_dict(snap)
    return market


@app.get("/api/signals")
async def api_signals(limit: int = 20):
    b = get_bot()
    history = b.state.state.get("signal_history", [])
    recent = history[-limit:][::-1] if history else []
    return recent


@app.get("/api/news")
async def api_news():
    b = get_bot()
    return b.last_news


@app.get("/api/dashboard")
async def api_dashboard():
    b = get_bot()
    return b.get_dashboard_state()


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(STATIC_DIR, "favicon.ico"))


# Mount static files at /static
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
