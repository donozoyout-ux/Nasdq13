"""Railway ASGI entrypoint.

Extends the existing dashboard/bot FastAPI application with Railway-only API
features while preserving src.webapp as the shared application core.
"""
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from src.webapp import app, get_bot
from src.analysis.mtf_chart_analysis import MultiTimeframeChartAnalysisService
from src.analysis.chart_intelligence_v2 import ChartIntelligenceV2Service
from src.analysis.learning_engine import LearningEngine
from src.analysis.learning_case_analytics import LearningCaseAnalytics
from src.analysis.adaptive_learning import AdaptiveLearningEngine
from src.analysis.weekly_focus import WeeklyFocusEngine
from src.analysis.smallcap_scanner import SmallCapScanner, SmallCapCandidate
from src.state.manager import StateManager
from src.utils.logger import get_logger

logger = get_logger("railway_app")
mtf_chart_service = MultiTimeframeChartAnalysisService()
chart_intelligence_v2 = ChartIntelligenceV2Service()


# ---------------------------------------------------------------------------
# State compatibility for Weekly Focus 20
# ---------------------------------------------------------------------------
# StateManager's normal disk loader already keeps unknown keys. GitHub restore
# historically whitelisted known top-level keys, so preserve weekly_focus there
# too without changing the canonical state manager implementation.
if not getattr(StateManager, "_weekly_focus_load_patch", False):
    _base_load_raw = StateManager.load_raw

    def _weekly_focus_load_raw(self, raw):
        weekly_focus = raw.get("weekly_focus") if isinstance(raw, dict) else None
        _base_load_raw(self, raw)
        if isinstance(weekly_focus, dict):
            self.state["weekly_focus"] = weekly_focus
            self.save()

    StateManager.load_raw = _weekly_focus_load_raw
    StateManager._weekly_focus_load_patch = True


# ---------------------------------------------------------------------------
# Adaptive Learning V1 runtime hook
# ---------------------------------------------------------------------------
# The persistent bot is created later by src.webapp's lifespan. Wrapping
# screen_setups here lets Railway apply the learned overlay before watchlist,
# reports and chart analysis are built, without changing the base strategy
# formulas inside smallcap_scanner.py.
if not getattr(SmallCapScanner, "_adaptive_learning_runtime_patch", False):
    _base_screen_setups = SmallCapScanner.screen_setups

    async def _adaptive_screen_setups(self, *args, **kwargs):
        candidates, universe = await _base_screen_setups(self, *args, **kwargs)
        try:
            b = get_bot()
            state = b.state.state if b is not None else {}
            config = b.config if b is not None else getattr(self, "config", {})
            engine = AdaptiveLearningEngine(state, config)
            ranked, status = engine.rank_candidates(candidates)
            if b is not None:
                b.adaptive_learning_status = status
            if status.get("live_reranking_active"):
                logger.info(
                    "Adaptive Learning ACTIVE: %s completed cases, %s approved factors",
                    status.get("completed_cases"),
                    status.get("approved_factor_count"),
                )
            else:
                logger.info(
                    "Adaptive Learning %s: %s/%s completed cases",
                    status.get("stage"),
                    status.get("completed_cases", 0),
                    status.get("guardrails", {}).get("min_completed_cases", 100),
                )
            return ranked, universe
        except Exception:
            # Learning is an overlay. A learning failure must never break the
            # canonical scanner or prevent candidates from being produced.
            logger.exception("Adaptive Learning overlay failed; base ranking preserved")
            return candidates, universe

    SmallCapScanner.screen_setups = _adaptive_screen_setups
    SmallCapScanner._adaptive_learning_runtime_patch = True


# Expose adaptive metadata in the existing candidate dict without changing the
# canonical setup_score field used for audit/debug comparisons.
if not getattr(SmallCapCandidate, "_adaptive_learning_to_dict_patch", False):
    _base_candidate_to_dict = SmallCapCandidate.to_dict

    def _adaptive_candidate_to_dict(self):
        data = _base_candidate_to_dict(self)
        base_score = getattr(self, "adaptive_base_score", self.setup_score)
        adaptive_score = getattr(self, "adaptive_score", self.setup_score)
        adjustment = getattr(self, "adaptive_adjustment", 0.0)
        data.update({
            "base_setup_score": round(float(base_score), 1),
            "adaptive_score": round(float(adaptive_score), 1),
            "adaptive_adjustment": round(float(adjustment), 2),
            "adaptive_learning_applied": bool(getattr(self, "adaptive_learning_applied", False)),
            "adaptive_factors": getattr(self, "adaptive_factors", []),
        })
        if hasattr(self, "weekly_selection_score"):
            data["weekly_selection_score"] = round(float(getattr(self, "weekly_selection_score")), 1)
        if hasattr(self, "weekly_rank"):
            data["weekly_rank"] = int(getattr(self, "weekly_rank"))
        return data

    SmallCapCandidate.to_dict = _adaptive_candidate_to_dict
    SmallCapCandidate._adaptive_learning_to_dict_patch = True


# ---------------------------------------------------------------------------
# Weekly Focus 20 runtime hook
# ---------------------------------------------------------------------------
# First scan of a new ISO week: temporarily let the canonical scanner score the
# whole valid universe, choose the 20 best focus names, and lock the list.
# Remaining scans that week: refresh only those same symbols. News enrichment,
# 1H/15M triggers, chart reading and learning therefore spend their effort on
# the focus list instead of 250 names.
if not getattr(SmallCapScanner, "_weekly_focus_runtime_patch", False):
    _focus_base_screen_setups = SmallCapScanner.screen_setups

    async def _weekly_focus_screen_setups(self, *args, **kwargs):
        b = get_bot()
        state = b.state.state if b is not None else {}
        config = b.config if b is not None else getattr(self, "config", {})
        focus = WeeklyFocusEngine(state, config)

        if not focus.enabled or b is None:
            return await _focus_base_screen_setups(self, *args, **kwargs)

        # Rule-based chart reading should cover all 20 focus names. LLM/vision
        # remains separately budget-capped by daily_budget_calls.
        chart_cfg = config.setdefault("ai_report", {}).setdefault("chart_analysis", {})
        chart_cfg["top_n"] = max(int(chart_cfg.get("top_n", 3)), focus.size)

        if focus.has_current_week():
            candidates, universe = await focus.refresh_focus(self)
            try:
                adaptive = AdaptiveLearningEngine(state, config)
                candidates, adaptive_status = adaptive.rank_candidates(candidates)
                b.adaptive_learning_status = adaptive_status
            except Exception:
                logger.exception("Adaptive overlay skipped during Focus 20 refresh")

            b.weekly_focus_status = {
                "stage": "LOCKED",
                "week_key": focus.week_key(),
                "focus_size": len(candidates),
                "universe_size": (focus.current_record() or {}).get("universe_size", len(universe)),
            }
            logger.info(
                "Weekly Focus %s locked: refreshing %s symbols only",
                focus.week_key(), len(candidates),
            )
            return candidates, universe

        # New week discovery: score every technically valid name, not only those
        # above the normal daily min_setup_score, then lock the best 20.
        old_min = self.min_setup_score
        self.min_setup_score = 0.0
        try:
            candidates, universe = await _focus_base_screen_setups(self, *args, **kwargs)
        finally:
            self.min_setup_score = old_min

        selected, record = focus.create_week(candidates, universe)
        b.weekly_focus_status = {
            "stage": "CREATED",
            "week_key": record.get("week_key"),
            "focus_size": len(selected),
            "universe_size": len(universe),
        }
        b.state.save()
        try:
            b._backup_state_async()
        except Exception:
            logger.debug("Weekly Focus state backup deferred to normal scan persistence")

        logger.info(
            "Weekly Focus %s CREATED: %s/%s symbols selected",
            record.get("week_key"), len(selected), len(universe),
        )
        return selected, universe

    SmallCapScanner.screen_setups = _weekly_focus_screen_setups
    SmallCapScanner._weekly_focus_runtime_patch = True


@app.get("/api/mtf-chart/{symbol}")
async def api_mtf_chart_analysis(symbol: str):
    """Return 1D -> 1H -> 15M top-down chart interpretation for one stock."""
    try:
        return await mtf_chart_service.analyze(symbol=symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected MTF chart analysis failure for %s", symbol)
        raise HTTPException(status_code=500, detail="multi-timeframe chart analysis failed") from exc


@app.get("/api/chart-v2/{symbol}")
async def api_chart_intelligence_v2(symbol: str, timeframe: str = "15m"):
    """Return deep, explainable price-action diagnostics without changing live ranking."""
    try:
        return await chart_intelligence_v2.analyze(symbol=symbol, timeframe=timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected Chart Intelligence V2 failure for %s %s", symbol, timeframe)
        raise HTTPException(status_code=500, detail="chart intelligence v2 failed") from exc


@app.get("/api/weekly-focus")
async def api_weekly_focus():
    """Return this week's locked Focus 20 with latest technical/news state."""
    b = get_bot()
    if b is None:
        return WeeklyFocusEngine({}, {}).dashboard_payload()
    try:
        return WeeklyFocusEngine(b.state.state, b.config).dashboard_payload(
            current_candidates=b.smallcap_candidates,
            news=b.last_news,
            chart_analyses=b.last_chart_analyses,
        )
    except Exception as exc:
        logger.exception("Unexpected Weekly Focus API failure")
        raise HTTPException(status_code=500, detail="weekly focus failed") from exc


@app.get("/focus", response_class=HTMLResponse)
async def weekly_focus_page():
    """Standalone Focus 20 board; root-dashboard integration can remain lightweight."""
    return HTMLResponse(content=r'''<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NASDQ13 · Weekly Focus 20</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#070b12;color:#e5e7eb;font-family:Inter,system-ui,-apple-system,sans-serif}.wrap{max-width:1450px;margin:auto;padding:24px}.top{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}.title{font-size:28px;font-weight:900}.sub{color:#7c8aa5;font-size:13px;margin-top:6px}.back{color:#93c5fd;text-decoration:none;border:1px solid #1f3350;padding:9px 12px;border-radius:9px}.stats{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;margin:18px 0}.stat,.card{background:#0c121d;border:1px solid #172235;border-radius:13px}.stat{padding:13px}.sk{font-size:10px;color:#63708a;text-transform:uppercase;letter-spacing:.08em}.sv{font-size:20px;font-weight:900;margin-top:4px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{padding:15px;position:relative}.rank{position:absolute;right:14px;top:13px;color:#536178;font-weight:900}.sym{font-size:20px;font-weight:950}.name{font-size:11px;color:#64748b;margin-top:2px}.badge{display:inline-block;margin-top:8px;padding:4px 8px;border-radius:999px;font-size:10px;font-weight:900}.TRIGGERED{background:#064e3b;color:#6ee7b7}.BREAKOUT_READY{background:#4c1d95;color:#c4b5fd}.SETUP{background:#78350f;color:#fcd34d}.WATCH{background:#172554;color:#93c5fd}.COLD{background:#27272a;color:#a1a1aa}.FAILED{background:#450a0a;color:#fca5a5}.bar{height:6px;background:#151e2d;border-radius:99px;overflow:hidden;margin:10px 0}.fill{height:100%;background:linear-gradient(90deg,#2563eb,#22c55e)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.m{background:#090e17;border:1px solid #141d2b;border-radius:8px;padding:8px}.mk{font-size:9px;color:#5f6b80;text-transform:uppercase}.mv{font-size:13px;font-weight:800;margin-top:3px}.levels{margin-top:10px;font-size:11px;color:#8b98ae;display:flex;gap:14px;flex-wrap:wrap}.news{margin-top:10px;padding:9px;background:#090e17;border-radius:8px;color:#9aa8bd;font-size:11px;line-height:1.4}.links{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}.links a{font-size:10px;text-decoration:none;color:#bfdbfe;border:1px solid #22324c;padding:6px 8px;border-radius:7px}.loading{padding:40px;text-align:center;color:#64748b}@media(max-width:900px){.grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.metrics{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="wrap"><div class="top"><div><div class="title">🎯 BU HAFTANIN FOCUS 20</div><div class="sub" id="sub">250 hisse haftalık keşif · 20 hisse derin takip · 1D → 1H → 15M</div></div><a class="back" href="/">← Ana Dashboard</a></div><div id="stats" class="stats"></div><div id="grid" class="grid"><div class="loading">Focus 20 hazırlanıyor...</div></div></div>
<script>
const f=(x,d=0)=>Number.isFinite(Number(x))?Number(x):d;const fmt=(x,n=1)=>Number.isFinite(Number(x))?Number(x).toFixed(n):'-';
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function load(){try{const r=await fetch('/api/weekly-focus',{cache:'no-store'}),d=await r.json();if(!d.active){document.getElementById('grid').innerHTML='<div class="loading">'+esc(d.message||'Liste henüz oluşmadı')+'</div>';return}document.getElementById('sub').textContent=`${d.week_key} · ${d.universe_size} hisseden ${d.focus_size} odak hisse · liste hafta boyunca sabit`;
const c=d.status_counts||{};document.getElementById('stats').innerHTML=[['Focus',d.focus_size],['Tetik',c.TRIGGERED||0],['Kırılıma Hazır',c.BREAKOUT_READY||0],['Setup',c.SETUP||0],['Son Güncelleme',(d.last_refresh_at||'').replace('T',' ').slice(0,16)]].map(x=>`<div class="stat"><div class="sk">${esc(x[0])}</div><div class="sv">${esc(x[1])}</div></div>`).join('');
document.getElementById('grid').innerHTML=(d.items||[]).map((x,i)=>{const s=x.weekly_status||'WATCH',score=f(x.weekly_live_score),headline=x.news_headline||x.chart_comment||'Henüz öne çıkan haber/yorum yok.';return `<div class="card"><div class="rank">#${i+1}</div><div class="sym">${esc(x.symbol)}</div><div class="name">${esc(x.name||'')}</div><span class="badge ${esc(s)}">${esc(s)}</span><div class="bar"><div class="fill" style="width:${Math.max(0,Math.min(100,score))}%"></div></div><div class="metrics"><div class="m"><div class="mk">Focus Skoru</div><div class="mv">${fmt(score)}/100</div></div><div class="m"><div class="mk">Setup</div><div class="mv">${fmt(x.setup_score)}</div></div><div class="m"><div class="mk">Öngörü</div><div class="mv">${fmt(x.anticipation_score)}</div></div><div class="m"><div class="mk">RVOL</div><div class="mv">${fmt(x.vol_ratio,2)}x</div></div><div class="m"><div class="mk">RSI</div><div class="mv">${fmt(x.rsi_14)}</div></div><div class="m"><div class="mk">15M Tetik</div><div class="mv">${esc(x.trigger_type||'-')}</div></div><div class="m"><div class="mk">RS 4W</div><div class="mv">${fmt(x.rs_4w)}%</div></div><div class="m"><div class="mk">Squeeze</div><div class="mv">${f(x.squeeze_days)} gün</div></div></div><div class="levels"><span>Destek: <b>${fmt(x.support_pivot,2)}</b></span><span>Direnç: <b>${fmt(x.resistance_pivot||x.donchian_upper,2)}</b></span><span>Dirence mesafe: <b>${fmt(x.dist_to_resistance_pct,2)}%</b></span></div><div class="news">📰 ${esc(headline).slice(0,360)}</div><div class="links"><a target="_blank" href="/api/mtf-chart/${encodeURIComponent(x.symbol)}">1D→1H→15M</a><a target="_blank" href="/api/chart-v2/${encodeURIComponent(x.symbol)}?timeframe=15m">Chart V2 15M</a><a target="_blank" href="/api/learning/${encodeURIComponent(x.symbol)}">Hata Hafızası</a></div></div>`}).join('');}catch(e){document.getElementById('grid').innerHTML='<div class="loading">Focus 20 yüklenemedi: '+esc(e.message)+'</div>'}}load();setInterval(load,60000);
</script></body></html>''')


@app.get("/api/learning-report")
async def api_learning_report():
    """Aggregate lessons learned from resolved live daily prediction outcomes."""
    b = get_bot()
    state = b.state.state if b is not None else {}
    try:
        return LearningEngine(state).report()
    except Exception as exc:
        logger.exception("Unexpected learning report failure")
        raise HTTPException(status_code=500, detail="learning report failed") from exc


@app.get("/api/learning/{symbol}")
async def api_symbol_learning(symbol: str):
    """Return shadow-only learned evidence relevant to the selected stock."""
    clean = (symbol or "").upper().strip()
    if not clean or len(clean) > 20:
        raise HTTPException(status_code=400, detail="invalid symbol")

    b = get_bot()
    state = b.state.state if b is not None else {}
    candidate = None
    if b is not None:
        for row in b.smallcap_candidates or []:
            if str(row.get("symbol") or "").upper() == clean:
                candidate = row
                break
        if candidate is None and b.last_weekly_report:
            for row in b.last_weekly_report.get("candidates", []) or []:
                if str(row.get("symbol") or "").upper() == clean:
                    candidate = row
                    break

    try:
        return LearningEngine(state).symbol_report(clean, candidate)
    except Exception as exc:
        logger.exception("Unexpected symbol learning failure for %s", clean)
        raise HTTPException(status_code=500, detail="symbol learning failed") from exc


@app.get("/api/learning-cases-report")
async def api_learning_cases_report():
    """Return scan-observed MFE/MAE, horizon and walk-forward case analytics."""
    b = get_bot()
    state = b.state.state if b is not None else {}
    try:
        return LearningCaseAnalytics(state).report()
    except Exception as exc:
        logger.exception("Unexpected learning case report failure")
        raise HTTPException(status_code=500, detail="learning case report failed") from exc


@app.get("/api/learning-cases/{symbol}")
async def api_symbol_learning_cases(symbol: str):
    """Return reconstructed historical learning cases for one stock."""
    clean = (symbol or "").upper().strip()
    if not clean or len(clean) > 20:
        raise HTTPException(status_code=400, detail="invalid symbol")
    b = get_bot()
    state = b.state.state if b is not None else {}
    try:
        return LearningCaseAnalytics(state).report(clean)
    except Exception as exc:
        logger.exception("Unexpected symbol learning case failure for %s", clean)
        raise HTTPException(status_code=500, detail="symbol learning case report failed") from exc


@app.get("/api/adaptive-learning-status")
async def api_adaptive_learning_status():
    """Show whether Adaptive Learning is collecting, validating or actively re-ranking."""
    b = get_bot()
    try:
        if b is not None and getattr(b, "adaptive_learning_status", None):
            return b.adaptive_learning_status
        state = b.state.state if b is not None else {}
        config = b.config if b is not None else {}
        return AdaptiveLearningEngine(state, config).status()
    except Exception as exc:
        logger.exception("Unexpected adaptive learning status failure")
        raise HTTPException(status_code=500, detail="adaptive learning status failed") from exc
