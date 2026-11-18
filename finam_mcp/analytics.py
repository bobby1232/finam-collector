from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_bar(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": parse_timestamp(bar["timestamp"]).isoformat(),
        "open": as_float(bar.get("open")),
        "high": as_float(bar.get("high")),
        "low": as_float(bar.get("low")),
        "close": as_float(bar.get("close")),
        "volume": as_float(bar.get("volume")),
        "source": bar.get("source"),
        "updated_at": (
            parse_timestamp(bar["updated_at"]).isoformat() if bar.get("updated_at") else None
        ),
    }


def is_complete(bar: dict[str, Any], interval: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return (
        parse_timestamp(bar["timestamp"]).timestamp() + INTERVAL_SECONDS[interval]
        <= now.timestamp()
    )


def completed_bars(
    bars: list[dict[str, Any]], interval: str, now: datetime | None = None
) -> tuple[list[dict[str, Any]], bool]:
    ordered = sorted((normalize_bar(x) for x in bars), key=lambda x: x["timestamp"])
    removed_incomplete = bool(ordered and not is_complete(ordered[-1], interval, now))
    return (ordered[:-1] if removed_incomplete else ordered), removed_incomplete


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def wilder_rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [b - a for a, b in zip(values[:-1], values[1:], strict=True)]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def wilder_atr(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(bars) <= period:
        return None
    true_ranges: list[float] = []
    previous = float(bars[0]["close"])
    for bar in bars[1:]:
        high, low = float(bar["high"]), float(bar["low"])
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = float(bar["close"])
    result = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        result = (result * (period - 1) + value) / period
    return result


def technical_analysis(bars: list[dict[str, Any]], interval: str) -> dict[str, Any]:
    closes = [float(x["close"]) for x in bars]
    current = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    rsi14 = wilder_rsi(closes, 14)
    atr14 = wilder_atr(bars, 14)
    recent = bars[-20:]
    support = min(float(x["low"]) for x in recent)
    resistance = max(float(x["high"]) for x in recent)
    if ema20 is None or ema50 is None:
        trend = "insufficient_data"
    elif current > ema20 > ema50:
        trend = "up"
    elif current < ema20 < ema50:
        trend = "down"
    else:
        trend = "mixed"
    return {
        "timeframe": interval,
        "last_completed_candle": bars[-1]["timestamp"],
        "close": current,
        "rsi14": round(rsi14, 4) if rsi14 is not None else None,
        "ema20": round(ema20, 4) if ema20 is not None else None,
        "ema50": round(ema50, 4) if ema50 is not None else None,
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "trend": trend,
        "support_20": support,
        "resistance_20": resistance,
        "distance_to_ema20": round(current - ema20, 4) if ema20 is not None else None,
        "oversold": rsi14 is not None and rsi14 < 30,
        "overbought": rsi14 is not None and rsi14 > 70,
        "candles_used": len(bars),
        "methodology": (
            "Wilder RSI14 and ATR14; EMA20/EMA50; support/resistance are the lowest/highest "
            "levels across 20 completed candles. Descriptive analytics only, not investment advice."
        ),
    }
