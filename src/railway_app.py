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
from src.utils.logger import get_logger

logger = get_logger("railway_app")
mtf_chart_service = MultiTimeframeChartAnalysisService()
chart_intelligence_v2 = ChartIntelligenceV2Service()


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
