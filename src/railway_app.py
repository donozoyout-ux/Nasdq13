"""Railway ASGI entrypoint.

Extends the existing dashboard/bot FastAPI application with Railway-only API
features while preserving src.webapp as the shared application core.
"""
from fastapi import HTTPException

from src.webapp import app
from src.analysis.mtf_chart_analysis import MultiTimeframeChartAnalysisService
from src.analysis.chart_intelligence_v2 import ChartIntelligenceV2Service
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
