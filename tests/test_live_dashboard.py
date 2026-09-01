from src.live_dashboard import build_today_predictions


def candidate(symbol="AAA", setup=70, anticipation=80, trigger=20):
    return {
        "symbol": symbol,
        "name": f"{symbol} Inc",
        "price": 12.5,
        "change_pct": 1.2,
        "setup_type": "squeeze",
        "setup_score": setup,
        "anticipation_score": anticipation,
        "trigger_score": trigger,
        "trigger_type": "near" if anticipation >= 75 else None,
        "expect_horizon": "1-2 seans",
        "trade_plan": {"limit": 12.8, "target": 14.2, "stop": 11.9},
        "budget_plan": {"shares": 3, "position_usd": 38.4},
        "reasons": ["dirence yakın", "volatilite daralıyor"],
        "trigger_reasons": [],
    }


def test_today_predictions_falls_back_to_latest_successful_scan():
    data = {
        "status": {"time_tr": "2026-09-01 14:00 TRT"},
        "smallcap": {
            "candidates": [],
            "scan_history": [
                {"time": "2026-08-31T18:00:00", "candidates": [candidate("OLD")]},
                {"time": "2026-09-01T10:00:00", "candidates": [candidate("NEW")]},
            ],
        },
    }

    result = build_today_predictions(data)

    assert result["source"] == "last_successful_scan"
    assert result["source_time"] == "2026-09-01T10:00:00"
    assert result["items"][0]["symbol"] == "NEW"
    assert result["count"] == 1


def test_live_candidates_take_priority_and_are_ranked():
    weak = candidate("WEAK", setup=55, anticipation=50, trigger=0)
    strong = candidate("STRONG", setup=85, anticipation=90, trigger=70)
    data = {
        "status": {"time_tr": "2026-09-01 14:00 TRT"},
        "smallcap": {
            "last_scan_at": "2026-09-01T10:30:00",
            "candidates": [weak, strong],
            "scan_history": [{"time": "old", "candidates": [candidate("OLD")]}],
        },
    }

    result = build_today_predictions(data)

    assert result["source"] == "live_scan"
    assert [item["symbol"] for item in result["items"]] == ["STRONG", "WEAK"]
    assert result["items"][0]["status"] == "ÇOK YAKIN"
    assert result["items"][0]["entry"] == 12.8
    assert 0 <= result["items"][0]["confidence_score"] <= 100
    assert "olasılığı değildir" in result["note"]
