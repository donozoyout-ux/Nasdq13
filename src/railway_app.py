"""Railway ASGI entrypoint.

Extends the existing dashboard/bot FastAPI application with Railway-only API
features while preserving src.webapp as the shared application core.
"""
from fastapi import HTTPException

from src.webapp import app, get_bot
from src.analysis.mtf_chart_analysis import MultiTimeframeChartAnalysisService
from src.analysis.chart_intelligence_v2 import ChartIntelligenceV2Service
from src.analysis.learning_engine import LearningEngine
from src.analysis.learning_case_analytics import LearningCaseAnalytics
from src.analysis.adaptive_learning import AdaptiveLearningEngine
from src.analysis.smallcap_scanner import SmallCapScanner, SmallCapCandidate
from src.utils.logger import get_logger

logger = get_logger("railway_app")
mtf_chart_service = MultiTimeframeChartAnalysisService()
chart_intelligence_v2 = ChartIntelligenceV2Service()


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
        return data

    SmallCapCandidate.to_dict = _adaptive_candidate_to_dict
    SmallCapCandidate._adaptive_learning_to_dict_patch = True


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
