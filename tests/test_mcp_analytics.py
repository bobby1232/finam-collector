from datetime import UTC, datetime, timedelta

from finam_mcp.analytics import completed_bars, ema, technical_analysis, wilder_atr, wilder_rsi


def make_bars(count: int, *, last_incomplete: bool = False):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    first = now - timedelta(minutes=(count - 1 if last_incomplete else count))
    return [
        {
            "timestamp": (first + timedelta(minutes=i)).isoformat(),
            "open": 100 + i,
            "high": 102 + i,
            "low": 99 + i,
            "close": 101 + i,
            "volume": 10,
            "source": "finam",
            "updated_at": now.isoformat(),
        }
        for i in range(count)
    ]


def test_wilder_indicators_and_ema_are_deterministic():
    bars = make_bars(60)
    closes = [float(x["close"]) for x in bars]
    assert wilder_rsi(closes, 14) == 100.0
    assert ema(closes, 20) is not None
    assert wilder_atr(bars, 14) == 3.0


def test_incomplete_current_candle_is_excluded():
    bars = make_bars(20, last_incomplete=True)
    complete, excluded = completed_bars(bars, "1m")
    assert excluded is True
    assert len(complete) == 19


def test_analysis_is_descriptive_and_uses_completed_data():
    result = technical_analysis(make_bars(80), "1m")
    assert result["trend"] == "up"
    assert result["rsi14"] == 100.0
    assert result["candles_used"] == 80
    assert "not investment advice" in result["methodology"].lower()
