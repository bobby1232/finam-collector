from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import pstdev

from .db import Database


def _f(v):
    return float(v) if v is not None else None


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
    return e


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) <= period:
        return None
    trs = []
    prev_close = _f(bars[0]["close"])
    for b in bars[1:]:
        hi, lo, cl = _f(b["high"]), _f(b["low"]), _f(b["close"])
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = cl
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def realized_vol(values: list[float], lookback: int) -> float | None:
    if len(values) < lookback + 1:
        return None
    vals = values[-(lookback + 1):]
    rets = [math.log(b / a) for a, b in zip(vals[:-1], vals[1:]) if a > 0 and b > 0]
    return pstdev(rets) * 100 if len(rets) >= 2 else None


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return max(0.0, center - half), min(1.0, center + half)



def ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    e = sum(values[:period]) / period
    out[period - 1] = e
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        e = alpha * values[i] + (1 - alpha) * e
        out[i] = e
    return out


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def barrier_stats_indices(bars: list[dict], indices: list[int], horizon_bars: int, target: float = 500.0) -> dict:
    wins = lower_first = same_bar = no_decisive = upper_hit = 0
    hit_times: list[int] = []
    valid = 0
    for i in indices:
        if i + horizon_bars + 1 >= len(bars):
            continue
        valid += 1
        entry = _f(bars[i]["close"])
        outcome = None
        upper_first_minutes = None
        for j in range(i + 1, i + 1 + horizon_bars):
            hi, lo = _f(bars[j]["high"]), _f(bars[j]["low"])
            up, dn = hi >= entry + target, lo <= entry - target
            if up and upper_first_minutes is None:
                upper_first_minutes = (j - i) * 15
            if outcome is None and up and dn:
                outcome = "same"
                same_bar += 1
            elif outcome is None and up:
                outcome = "up"
                wins += 1
                hit_times.append((j - i) * 15)
            elif outcome is None and dn:
                outcome = "down"
                lower_first += 1
        if upper_first_minutes is not None:
            upper_hit += 1
        if outcome is None:
            no_decisive += 1
    lo, hi = wilson(wins, valid)
    hit_lo, hit_hi = wilson(upper_hit, valid)
    hit_times.sort()
    return {
        "samples": valid,
        "plus500_before_minus500": wins / valid if valid else None,
        "plus500_before_minus500_ci95": [lo, hi],
        "plus500_anytime": upper_hit / valid if valid else None,
        "plus500_anytime_ci95": [hit_lo, hit_hi],
        "lower_first": lower_first,
        "same_bar_ambiguous": same_bar,
        "no_decisive_hit": no_decisive,
        "median_minutes_to_plus500_when_first": hit_times[len(hit_times)//2] if hit_times else None,
    }

def barrier_stats(bars: list[dict], horizon_bars: int, target: float = 500.0, stride: int = 4) -> dict:
    wins = 0
    lower_first = 0
    no_upper = 0
    same_bar = 0
    upper_hit = 0
    hit_times = []
    samples = 0

    for i in range(0, max(0, len(bars) - horizon_bars - 1), stride):
        entry = _f(bars[i]["close"])
        samples += 1
        outcome = None
        upper_first_minutes = None
        for j in range(i + 1, min(len(bars), i + 1 + horizon_bars)):
            hi = _f(bars[j]["high"])
            lo = _f(bars[j]["low"])
            up = hi >= entry + target
            dn = lo <= entry - target
            if up and upper_first_minutes is None:
                upper_first_minutes = (j - i) * 15
            if outcome is None and up and dn:
                outcome = "same"
                same_bar += 1
            elif outcome is None and up:
                outcome = "up"
                wins += 1
                hit_times.append((j - i) * 15)
            elif outcome is None and dn:
                outcome = "down"
                lower_first += 1

        if upper_first_minutes is not None:
            upper_hit += 1
        if outcome is None:
            no_upper += 1

    lo, hi = wilson(wins, samples)
    hit_lo, hit_hi = wilson(upper_hit, samples)
    hit_times.sort()
    median_minutes = hit_times[len(hit_times)//2] if hit_times else None
    return {
        "samples": samples,
        "target_points": target,
        "plus500_before_minus500": wins / samples if samples else None,
        "plus500_before_minus500_ci95": [lo, hi],
        "plus500_anytime": upper_hit / samples if samples else None,
        "plus500_anytime_ci95": [hit_lo, hit_hi],
        "lower_first": lower_first,
        "same_bar_ambiguous": same_bar,
        "no_decisive_hit": no_upper,
        "median_minutes_to_plus500_when_first": median_minutes,
        "sampling": "15m bars, one start per hour",
    }


def build_si_analysis(db: Database, symbol: str = "SI@CONT") -> dict:
    now = datetime.now(timezone.utc)
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    bars15 = db.chart_bars(symbol, "TIME_FRAME_M1", start, now, 10000, "15m")
    bars1h = db.chart_bars(symbol, "TIME_FRAME_M1", start, now, 10000, "1h")
    if not bars15 or not bars1h:
        return {"error": "not enough data"}

    closes15 = [_f(x["close"]) for x in bars15]
    closes1h = [_f(x["close"]) for x in bars1h]
    ema20s = ema_series(closes15, 20)
    ema50s = ema_series(closes15, 50)
    rsi14s = rsi_series(closes15, 14)
    similar_indices = [
        i for i in range(0, len(bars15), 4)
        if rsi14s[i] is not None
        and ema20s[i] is not None
        and ema50s[i] is not None
        and rsi14s[i] <= 30
        and closes15[i] < ema20s[i] < ema50s[i]
    ]

    latest = bars15[-1]
    latest_ts = latest["timestamp"]
    current_day = latest_ts.date()

    day_bars = [b for b in bars15 if b["timestamp"].date() == current_day]
    prev_dates = sorted({b["timestamp"].date() for b in bars15 if b["timestamp"].date() < current_day})
    prev_day = prev_dates[-1] if prev_dates else None
    prev_bars = [b for b in bars15 if prev_day and b["timestamp"].date() == prev_day]

    last20_dates = sorted({b["timestamp"].date() for b in bars15})[-20:]
    last20 = [b for b in bars15 if b["timestamp"].date() in last20_dates]

    def session_summary(xs):
        if not xs:
            return None
        return {
            "open": _f(xs[0]["open"]),
            "high": max(_f(x["high"]) for x in xs),
            "low": min(_f(x["low"]) for x in xs),
            "close": _f(xs[-1]["close"]),
            "volume": sum(_f(x["volume"]) or 0.0 for x in xs),
        }

    result = {
        "symbol": symbol,
        "generated_at": now.isoformat(),
        "latest": {
            "timestamp": latest_ts.isoformat(),
            "price": _f(latest["close"]),
            "volume_15m": _f(latest["volume"]),
        },
        "trend_15m": {
            "rsi14": rsi(closes15, 14),
            "ema20": ema(closes15, 20),
            "ema50": ema(closes15, 50),
            "atr14": atr(bars15, 14),
            "rv20_pct_per_bar": realized_vol(closes15, 20),
            "rv50_pct_per_bar": realized_vol(closes15, 50),
        },
        "trend_1h": {
            "rsi14": rsi(closes1h, 14),
            "ema20": ema(closes1h, 20),
            "ema50": ema(closes1h, 50),
            "atr14": atr(bars1h, 14),
            "rv20_pct_per_bar": realized_vol(closes1h, 20),
            "rv50_pct_per_bar": realized_vol(closes1h, 50),
        },
        "today": session_summary(day_bars),
        "previous_session": session_summary(prev_bars),
        "last20_sessions": {
            "high": max(_f(x["high"]) for x in last20),
            "low": min(_f(x["low"]) for x in last20),
        },
        "barrier_probabilities": {
            "unconditional": {
                "1_trading_day": barrier_stats(bars15, 56, 500.0, 4),
                "3_trading_days": barrier_stats(bars15, 56 * 3, 500.0, 4),
                "5_trading_days": barrier_stats(bars15, 56 * 5, 500.0, 4),
            },
            "similar_state_rsi_le_30_below_ema20_below_ema50": {
                "condition": "15m RSI14 <= 30 and close < EMA20 < EMA50; hourly sampling",
                "matching_starts": len(similar_indices),
                "1_trading_day": barrier_stats_indices(bars15, similar_indices, 56, 500.0),
                "3_trading_days": barrier_stats_indices(bars15, similar_indices, 56 * 3, 500.0),
                "5_trading_days": barrier_stats_indices(bars15, similar_indices, 56 * 5, 500.0),
            },
        },
    }

    px = result["latest"]["price"]
    a1 = result["trend_1h"]["atr14"]
    a15 = result["trend_15m"]["atr14"]
    result["target_context"] = {
        "target_points": 500,
        "target_pct": 500 / px * 100 if px else None,
        "target_in_atr_15m": 500 / a15 if a15 else None,
        "target_in_atr_1h": 500 / a1 if a1 else None,
        "up_target": px + 500 if px else None,
        "down_500": px - 500 if px else None,
    }
    return result


def build_technical_snapshot(db: Database, symbol: str) -> dict:
    """Lightweight technical snapshot for operational diagnostics."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=60)
    bars15 = db.chart_bars(symbol, "TIME_FRAME_M1", start, now, 10000, "15m")
    bars1h = db.chart_bars(symbol, "TIME_FRAME_M1", start, now, 10000, "1h")
    if not bars15 or not bars1h:
        return {"symbol": symbol, "error": "not enough data"}

    closes15 = [_f(x["close"]) for x in bars15]
    closes1h = [_f(x["close"]) for x in bars1h]
    latest = bars15[-1]
    latest_ts = latest["timestamp"]
    current_day = latest_ts.date()

    dates = sorted({b["timestamp"].date() for b in bars15})
    today_bars = [b for b in bars15 if b["timestamp"].date() == current_day]
    prev_day = dates[-2] if len(dates) >= 2 else None
    prev_bars = [b for b in bars15 if prev_day and b["timestamp"].date() == prev_day]

    def session_summary(xs):
        if not xs:
            return None
        return {
            "date": xs[0]["timestamp"].date().isoformat(),
            "open": _f(xs[0]["open"]),
            "high": max(_f(x["high"]) for x in xs),
            "low": min(_f(x["low"]) for x in xs),
            "close": _f(xs[-1]["close"]),
            "volume": sum(_f(x["volume"]) or 0.0 for x in xs),
        }

    sessions = []
    for d in dates[-20:]:
        xs = [b for b in bars15 if b["timestamp"].date() == d]
        s = session_summary(xs)
        if s:
            sessions.append(s)

    last5 = sessions[-5:]
    last20 = sessions[-20:]
    completed_sessions = [s for s in sessions if s["date"] != current_day.isoformat()]
    avg20_volume = (
        sum(s["volume"] for s in completed_sessions[-20:]) / len(completed_sessions[-20:])
        if completed_sessions
        else None
    )
    today = session_summary(today_bars)
    prev = session_summary(prev_bars)

    px = _f(latest["close"])
    e20_15 = ema(closes15, 20)
    e50_15 = ema(closes15, 50)
    e200_15 = ema(closes15, 200)
    e20_1h = ema(closes1h, 20)
    e50_1h = ema(closes1h, 50)
    e200_1h = ema(closes1h, 200)

    return {
        "symbol": symbol,
        "generated_at": now.isoformat(),
        "latest": {
            "timestamp": latest_ts.isoformat(),
            "price": px,
            "volume_15m": _f(latest["volume"]),
        },
        "trend_15m": {
            "rsi14": rsi(closes15, 14),
            "ema20": e20_15,
            "ema50": e50_15,
            "ema200": e200_15,
            "atr14": atr(bars15, 14),
            "price_vs_ema20_pct": ((px / e20_15) - 1) * 100 if px and e20_15 else None,
            "price_vs_ema50_pct": ((px / e50_15) - 1) * 100 if px and e50_15 else None,
        },
        "trend_1h": {
            "rsi14": rsi(closes1h, 14),
            "ema20": e20_1h,
            "ema50": e50_1h,
            "ema200": e200_1h,
            "atr14": atr(bars1h, 14),
            "price_vs_ema20_pct": ((px / e20_1h) - 1) * 100 if px and e20_1h else None,
            "price_vs_ema50_pct": ((px / e50_1h) - 1) * 100 if px and e50_1h else None,
        },
        "today": today,
        "previous_session": prev,
        "volume": {
            "avg_completed_session_volume_20d": avg20_volume,
            "today_vs_avg20": (today["volume"] / avg20_volume) if today and avg20_volume else None,
        },
        "levels": {
            "last5_high": max(s["high"] for s in last5) if last5 else None,
            "last5_low": min(s["low"] for s in last5) if last5 else None,
            "last20_high": max(s["high"] for s in last20) if last20 else None,
            "last20_low": min(s["low"] for s in last20) if last20 else None,
            "previous_high": prev["high"] if prev else None,
            "previous_low": prev["low"] if prev else None,
            "today_high": today["high"] if today else None,
            "today_low": today["low"] if today else None,
        },
        "sessions": last20,
    }


def build_si_extended_snapshot(db: Database, symbol: str = "SI@CONT") -> dict:
    """Current SI snapshot with price, volume, trend, momentum and nearby levels."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=90)
    bars15 = db.chart_bars(symbol, "TIME_FRAME_M1", start, now, 10000, "15m")
    bars1h = db.chart_bars(symbol, "TIME_FRAME_M1", start, now, 10000, "1h")
    if not bars15 or not bars1h:
        return {"symbol": symbol, "error": "not enough data"}

    c15 = [_f(x["close"]) for x in bars15]
    c1h = [_f(x["close"]) for x in bars1h]
    latest = bars15[-1]
    latest_ts = latest["timestamp"]
    current_day = latest_ts.date()
    dates = sorted({b["timestamp"].date() for b in bars15})

    def session_summary(xs):
        if not xs:
            return None
        return {
            "date": xs[0]["timestamp"].date().isoformat(),
            "open": _f(xs[0]["open"]),
            "high": max(_f(x["high"]) for x in xs),
            "low": min(_f(x["low"]) for x in xs),
            "close": _f(xs[-1]["close"]),
            "volume": sum(_f(x["volume"]) or 0.0 for x in xs),
        }

    sessions = []
    for d in dates[-30:]:
        s = session_summary([b for b in bars15 if b["timestamp"].date() == d])
        if s:
            sessions.append(s)
    today = session_summary([b for b in bars15 if b["timestamp"].date() == current_day])
    completed = [s for s in sessions if s["date"] != current_day.isoformat()]
    avg20v = sum(s["volume"] for s in completed[-20:]) / len(completed[-20:]) if completed else None
    avg5v = sum(s["volume"] for s in completed[-5:]) / len(completed[-5:]) if completed else None
    px = _f(latest["close"])

    def trend(vals, bars):
        e20, e50, e200 = ema(vals,20), ema(vals,50), ema(vals,200)
        return {
            "rsi14": rsi(vals,14), "ema20":e20, "ema50":e50, "ema200":e200,
            "atr14": atr(bars,14),
            "price_vs_ema20_pct": ((px/e20)-1)*100 if px and e20 else None,
            "price_vs_ema50_pct": ((px/e50)-1)*100 if px and e50 else None,
            "price_vs_ema200_pct": ((px/e200)-1)*100 if px and e200 else None,
        }

    last5=sessions[-5:]; last20=sessions[-20:]
    recent15=bars15[-32:]
    upvol=sum((_f(b["volume"]) or 0) for b in recent15 if _f(b["close"]) >= _f(b["open"]))
    downvol=sum((_f(b["volume"]) or 0) for b in recent15 if _f(b["close"]) < _f(b["open"]))

    return {
        "symbol":symbol,"generated_at":now.isoformat(),
        "latest":{"timestamp":latest_ts.isoformat(),"price":px,"volume_15m":_f(latest["volume"])},
        "trend_15m":trend(c15,bars15),"trend_1h":trend(c1h,bars1h),
        "today":today,
        "previous_session":completed[-1] if completed else None,
        "volume":{
            "avg20_completed":avg20v,"avg5_completed":avg5v,
            "today_vs_avg20": today["volume"]/avg20v if today and avg20v else None,
            "today_vs_avg5": today["volume"]/avg5v if today and avg5v else None,
            "last_32x15m_up_volume":upvol,"last_32x15m_down_volume":downvol,
            "down_up_ratio":downvol/upvol if upvol else None,
        },
        "levels":{
            "last5_high":max(s["high"] for s in last5) if last5 else None,
            "last5_low":min(s["low"] for s in last5) if last5 else None,
            "last20_high":max(s["high"] for s in last20) if last20 else None,
            "last20_low":min(s["low"] for s in last20) if last20 else None,
            "previous_high":completed[-1]["high"] if completed else None,
            "previous_low":completed[-1]["low"] if completed else None,
            "today_high":today["high"] if today else None,
            "today_low":today["low"] if today else None,
        },
        "sessions":sessions[-10:],
    }
