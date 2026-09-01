"""Railway Live Dashboard V2.

This module wraps the existing FastAPI application without changing the trading
engine.  It replaces only the root page and adds read-only live endpoints:

- /api/live                -> enriched dashboard JSON
- /api/live/stream         -> Server-Sent Events (near real-time UI updates)
- /api/today-predictions   -> always-available ranked daily candidates
- /api/excel/today.csv     -> Excel/Power Query friendly daily candidates
- /api/excel/market.csv    -> Excel/Power Query friendly market snapshots
- /api/excel/signals.csv   -> Excel/Power Query friendly signal history
- /legacy                  -> previous dashboard

CSV endpoints intentionally use stable URLs so Microsoft Excel can connect with
Data > From Web and refresh the same bot data source without a separate file
synchronisation process.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from src.webapp import app, get_bot, render_template


LIVE_TEMPLATE = "live_dashboard.html"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _latest_candidate_pool(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """Return candidates even when the current in-memory scan has not completed.

    The normal dashboard only reads ``smallcap.candidates``.  After a restart or
    between scan windows that can be empty even though a persisted successful
    scan exists.  For today's view we fall back to the newest non-empty scan
    history record, which is already part of bot state / GitHub restore.
    """
    smallcap = data.get("smallcap") or {}
    current = smallcap.get("candidates") or []
    if current:
        return list(current), "live_scan", smallcap.get("last_scan_at")

    history = smallcap.get("scan_history") or []
    for scan in reversed(history):
        candidates = scan.get("candidates") or []
        if candidates:
            return list(candidates), "last_successful_scan", scan.get("time")

    return [], "waiting_for_first_scan", None


def _prediction_score(candidate: Dict[str, Any]) -> float:
    """Ranking score, not a probability of profit.

    The strategy already exposes three 0-100 components.  Blending them gives a
    stable UI ordering while keeping the underlying strategy untouched.
    """
    setup = _num(candidate.get("setup_score"))
    anticipation = _num(candidate.get("anticipation_score"))
    trigger = _num(candidate.get("trigger_score"))
    score = setup * 0.45 + anticipation * 0.40 + trigger * 0.15
    return max(0.0, min(100.0, score))


def _prediction_status(candidate: Dict[str, Any]) -> str:
    trigger_type = str(candidate.get("trigger_type") or "").lower()
    anticipation = _num(candidate.get("anticipation_score"))
    if trigger_type == "breakout":
        return "KIRILIM"
    if trigger_type == "near" or anticipation >= 75:
        return "ÇOK YAKIN"
    if anticipation >= 60:
        return "YAKIN"
    return "İZLE"


def build_today_predictions(data: Dict[str, Any], limit: int = 8) -> Dict[str, Any]:
    pool, source, source_time = _latest_candidate_pool(data)

    ranked = sorted(
        pool,
        key=lambda c: (
            _prediction_score(c),
            _num(c.get("anticipation_score")),
            _num(c.get("setup_score")),
        ),
        reverse=True,
    )[: max(1, int(limit))]

    items: List[Dict[str, Any]] = []
    for rank, candidate in enumerate(ranked, 1):
        trade = candidate.get("trade_plan") or {}
        budget = candidate.get("budget_plan") or {}
        reasons: List[str] = []
        for raw in list(candidate.get("reasons") or []) + list(candidate.get("trigger_reasons") or []):
            text = str(raw).strip()
            if text and text not in reasons:
                reasons.append(text)

        items.append(
            {
                "rank": rank,
                "symbol": candidate.get("symbol") or "-",
                "name": candidate.get("name") or "",
                "price": _num(candidate.get("price")),
                "change_pct": _num(candidate.get("change_pct")),
                "setup_type": candidate.get("setup_type") or "watch",
                "setup_score": _num(candidate.get("setup_score")),
                "anticipation_score": _num(candidate.get("anticipation_score")),
                "trigger_score": _num(candidate.get("trigger_score")),
                "confidence_score": round(_prediction_score(candidate), 1),
                "status": _prediction_status(candidate),
                "horizon": candidate.get("expect_horizon") or "-",
                "entry": _num(trade.get("limit")),
                "target": _num(trade.get("target")),
                "stop": _num(trade.get("stop")),
                "shares": int(_num(budget.get("shares"))),
                "position_usd": _num(budget.get("position_usd")),
                "rsi": _num(candidate.get("rsi_14")),
                "volume_ratio": _num(candidate.get("vol_ratio"), 1.0),
                "resistance_distance_pct": _num(candidate.get("dist_to_resistance_pct")),
                "patterns": list(candidate.get("candle_patterns") or []),
                "reasons": reasons[:4],
            }
        )

    status = data.get("status") or {}
    return {
        "date_tr": str(status.get("time_tr") or "")[:10] or datetime.now(timezone.utc).date().isoformat(),
        "source": source,
        "source_time": source_time,
        "count": len(items),
        "items": items,
        "note": "Güven skoru strateji bileşenlerinin sıralama skorudur; gerçekleşme olasılığı değildir.",
    }


def _latest_market_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    market = data.get("market") or {}
    preferred = ["15m", "1h", "1d", "5m", "1m"]
    rows: List[Dict[str, Any]] = []

    for symbol, timeframes in market.items():
        timeframes = timeframes or {}
        chosen_name = None
        chosen = None
        for tf in preferred:
            if timeframes.get(tf):
                chosen_name = tf
                chosen = timeframes[tf]
                break
        if chosen is None:
            for tf, snap in timeframes.items():
                if snap:
                    chosen_name = tf
                    chosen = snap
                    break
        if not chosen:
            continue
        rows.append(
            {
                "symbol": symbol,
                "timeframe": chosen_name or "-",
                "price": _num(chosen.get("price")),
                "change_pct": _num(chosen.get("change_pct")),
                "score": _num(chosen.get("composite_score")),
                "rsi": _num(chosen.get("rsi_14")),
                "volume_ratio": _num(chosen.get("volume_ratio")),
                "breakout_up": bool(chosen.get("is_breakout_up")),
                "volume_spike": bool(chosen.get("is_volume_spike")),
            }
        )
    return rows


def build_live_payload(bot: Any) -> Dict[str, Any]:
    if bot is None:
        data: Dict[str, Any] = {
            "status": {"running": False, "healthy": False},
            "market": {},
            "signals": [],
            "history": [],
            "smallcap": {"candidates": [], "scan_history": [], "universe_size": 0},
            "predictions": {},
            "budget": {},
        }
    else:
        data = bot.get_dashboard_state()

    today = build_today_predictions(data)
    data["today_predictions"] = today
    data["market_ticker"] = _latest_market_rows(data)
    data["live_meta"] = {
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "transport": "sse",
        "excel": {
            "today": "/api/excel/today.csv",
            "market": "/api/excel/market.csv",
            "signals": "/api/excel/signals.csv",
        },
    }
    return data


def _csv_response(headers: Iterable[str], rows: Iterable[Iterable[Any]], filename: str) -> StreamingResponse:
    buffer = io.StringIO()
    # UTF-8 BOM keeps Turkish characters correct when Excel opens the file.
    buffer.write("\ufeff")
    writer = csv.writer(buffer, delimiter=",")
    writer.writerow(list(headers))
    for row in rows:
        writer.writerow(list(row))
    payload = buffer.getvalue().encode("utf-8")
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _remove_previous_root() -> None:
    kept = []
    for route in app.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if getattr(route, "path", None) == "/" and "GET" in methods:
            continue
        kept.append(route)
    app.router.routes[:] = kept


_remove_previous_root()


@app.get("/", response_class=HTMLResponse, name="live_dashboard")
async def live_dashboard(request: Request):
    return render_template(request, LIVE_TEMPLATE, {"request": request})


@app.get("/legacy", response_class=HTMLResponse)
async def legacy_dashboard(request: Request):
    return render_template(request, "dashboard.html", {"request": request})


@app.get("/api/live")
async def api_live():
    return JSONResponse(build_live_payload(get_bot()))


@app.get("/api/today-predictions")
async def api_today_predictions():
    payload = build_live_payload(get_bot())
    return JSONResponse(payload["today_predictions"])


@app.get("/api/live/stream")
async def api_live_stream():
    async def events():
        last_fingerprint = None
        while True:
            try:
                payload = build_live_payload(get_bot())
                compact = {
                    "scan": (payload.get("status") or {}).get("scan_count"),
                    "last": (payload.get("status") or {}).get("last_scan_at"),
                    "today": payload.get("today_predictions"),
                    "ticker": payload.get("market_ticker"),
                    "errors": (payload.get("status") or {}).get("error_count"),
                }
                fingerprint = json.dumps(compact, sort_keys=True, ensure_ascii=False, default=str)
                if fingerprint != last_fingerprint:
                    yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    last_fingerprint = fingerprint
                else:
                    yield ": keepalive\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(4)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/excel/today.csv")
async def excel_today():
    payload = build_live_payload(get_bot())
    predictions = (payload.get("today_predictions") or {}).get("items") or []
    headers = [
        "Sıra", "Sembol", "Şirket", "Durum", "Fiyat", "Değişim %",
        "Güven Skoru", "Setup", "Öngörü", "Trigger", "Horizon",
        "Giriş", "Hedef", "Stop", "Adet", "Pozisyon USD", "Nedenler",
    ]
    rows = [
        [
            p.get("rank"), p.get("symbol"), p.get("name"), p.get("status"),
            p.get("price"), p.get("change_pct"), p.get("confidence_score"),
            p.get("setup_score"), p.get("anticipation_score"), p.get("trigger_score"),
            p.get("horizon"), p.get("entry"), p.get("target"), p.get("stop"),
            p.get("shares"), p.get("position_usd"), " | ".join(p.get("reasons") or []),
        ]
        for p in predictions
    ]
    return _csv_response(headers, rows, "nasdaq_bugunun_tahminleri.csv")


@app.get("/api/excel/market.csv")
async def excel_market():
    payload = build_live_payload(get_bot())
    headers = ["Sembol", "Periyot", "Fiyat", "Değişim %", "Kompozit Skor", "RSI", "Hacim Katı", "Kırılım", "Hacim Patlaması"]
    rows = [
        [
            r.get("symbol"), r.get("timeframe"), r.get("price"), r.get("change_pct"),
            r.get("score"), r.get("rsi"), r.get("volume_ratio"),
            "EVET" if r.get("breakout_up") else "HAYIR",
            "EVET" if r.get("volume_spike") else "HAYIR",
        ]
        for r in payload.get("market_ticker") or []
    ]
    return _csv_response(headers, rows, "nasdaq_canli_piyasa.csv")


@app.get("/api/excel/signals.csv")
async def excel_signals():
    payload = build_live_payload(get_bot())
    history = payload.get("history") or []
    headers = ["Zaman", "Sembol", "Aksiyon", "Yön", "Fiyat", "Güç", "Durum", "Hedef", "Stop"]
    rows = [
        [
            h.get("timestamp"), h.get("symbol"), h.get("action"), h.get("direction"),
            h.get("price", h.get("entry_price")), h.get("strength"), h.get("status"),
            h.get("target_price"), h.get("stop_price"),
        ]
        for h in reversed(history)
    ]
    return _csv_response(headers, rows, "nasdaq_sinyal_gecmisi.csv")
