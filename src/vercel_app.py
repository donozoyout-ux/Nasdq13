"""Vercel-only FastAPI dashboard entrypoint.

This module intentionally avoids importing the long-running bot/analysis stack so
serverless invocations stay lightweight and cannot start duplicate workers.
Railway continues to run src.webapp/src.main for the persistent bot backend.
"""
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "web" / "templates"
STATIC_DIR = BASE_DIR / "web" / "static"

app = FastAPI(title="NASDAQ Signal Bot Dashboard")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"request": request},
    )


@app.get("/health")
async def health():
    return JSONResponse(
        {
            "status": "ok",
            "mode": "vercel-dashboard",
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/status")
async def api_status():
    return {
        "running": False,
        "healthy": True,
        "mode": "vercel-dashboard",
        "scan_count": 0,
        "error_count": 0,
        "last_scan_at": None,
        "scan_interval_seconds": 60,
        "symbols": [],
        "timeframes": [],
    }


@app.get("/api/market")
async def api_market():
    return {}


@app.get("/api/signals")
async def api_signals(limit: int = 20):
    return []


@app.get("/api/news")
async def api_news():
    return {}


@app.get("/api/dashboard")
async def api_dashboard():
    return {
        "status": {
            "running": False,
            "healthy": True,
            "mode": "vercel-dashboard",
            "scan_count": 0,
            "error_count": 0,
            "last_scan_at": None,
            "scan_interval_seconds": 60,
        },
        "config": {"symbols": [], "timeframes": [], "thresholds": {}},
        "market": {},
        "signals": [],
        "news": {},
        "stats": {},
        "history": [],
    }


@app.get("/api/weekly-report")
async def api_weekly_report():
    return {"report": None, "candidates": [], "index": {}}


@app.get("/api/performance")
async def api_performance():
    return {"win_rate": 0.0, "total_signals": 0, "wins": 0, "losses": 0}
