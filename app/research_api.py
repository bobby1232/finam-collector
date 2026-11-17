"""Read-only, token-protected Si oversold backtest API."""
import hmac
import os
from datetime import datetime, timezone
from statistics import mean, median

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query

router = APIRouter(prefix="/api/research", tags=["research"])
FRAMES = {"15m": 900, "1h": 3600, "1d": 86400}
MAX_ROWS = 200000


def authorize(authorization: str | None = Header(default=None)):
    secret = os.environ.get("RESEARCH_API_TOKEN", "")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Research API not configured")
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


def load_bars(symbol: str, frame: str):
    if symbol not in ("SI@CONT", "SiZ6@RTSX"):
        raise HTTPException(400, "Unsupported symbol")
    seconds = FRAMES[frame]
    # date_bin anchors UTC, avoids local timezone-dependent bucket boundaries.
    sql = """
    WITH buckets AS (
      SELECT date_bin(%s::interval, ts, '1970-01-01 00:00:00+00'::timestamptz) AS bucket,
             ts, open, high, low, close
      FROM candles WHERE ticker=%s AND timeframe='TIME_FRAME_M1'
    ), ranked AS (
      SELECT *, row_number() OVER (PARTITION BY bucket ORDER BY ts) AS rn_first,
                row_number() OVER (PARTITION BY bucket ORDER BY ts DESC) AS rn_last
      FROM buckets
    )
    SELECT bucket, max(open) FILTER (WHERE rn_first=1),
           max(high), min(low), max(close) FILTER (WHERE rn_last=1),
           count(*)
    FROM ranked GROUP BY bucket ORDER BY bucket DESC LIMIT %s
    """
    with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (f"{seconds} seconds", symbol, MAX_ROWS))
            rows = cur.fetchall()
    rows.reverse()
    return [(t, float(o), float(h), float(l), float(c), int(n)) for t,o,h,l,c,n in rows]


def rsi_wilder(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, period+1)]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, period+1)]
    g, l = sum(gains)/period, sum(losses)/period
    for i in range(period, len(closes)):
        if i > period:
            change = closes[i]-closes[i-1]
            g = (g*(period-1)+max(change,0))/period
            l = (l*(period-1)+max(-change,0))/period
        out[i] = 100 if l == 0 else 100 - 100/(1+g/l)
    return out


def stats(values):
    return {"mean": round(mean(values),2), "median": round(median(values),2),
            "min": round(min(values),2), "max": round(max(values),2)} if values else None


@router.get("/si/oversold", dependencies=[Depends(authorize)])
def oversold(
    symbol: str = Query("SI@CONT"),
    timeframe: str = Query("1h"),
    thresholds: str = Query("30,25,20"),
    horizons: str = Query("1,3,5,10"),
):
    if timeframe not in FRAMES:
        raise HTTPException(400, "timeframe must be 15m, 1h, 1d")
    try:
        levels = sorted(set(float(x) for x in thresholds.split(",")), reverse=True)
        steps = sorted(set(int(x) for x in horizons.split(",")))
    except ValueError:
        raise HTTPException(400, "Invalid thresholds or horizons")
    if not levels or not steps or any(x <= 0 or x >= 50 for x in levels) or any(x < 1 or x > 100 for x in steps):
        raise HTTPException(400, "Out of range")
    bars = load_bars(symbol, timeframe)
    if len(bars) < 30:
        raise HTTPException(422, "Insufficient historical bars")
    closes = [b[4] for b in bars]
    rsi = rsi_wilder(closes)
    max_step = max(steps)
    results = {}
    for threshold in levels:
        # Crossing from above is one signal, not one signal on every oversold candle.
        candidates = [i for i in range(15, len(bars)-max_step)
                      if rsi[i] is not None and rsi[i-1] is not None
                      and rsi[i] < threshold <= rsi[i-1]]
        # Non-overlapping forward windows prevent counting the same price move repeatedly.
        selected = []
        last = -max_step-1
        for i in candidates:
            if i > last + max_step:
                selected.append(i)
                last = i
        by_horizon = {}
        for h in steps:
            ups, downs, finishes = [], [], []
            for i in selected:
                entry = closes[i]  # close-of-signal reference, not an executable fill
                future = bars[i+1:i+h+1]
                ups.append(max(x[2] for x in future)-entry)
                downs.append(min(x[3] for x in future)-entry)
                finishes.append(future[-1][4]-entry)
            by_horizon[str(h)] = {
                "max_favorable_points": stats(ups),
                "max_adverse_points": stats(downs),
                "close_return_points": stats(finishes),
                "positive_close_pct": round(100*sum(x>0 for x in finishes)/len(finishes),2) if finishes else None,
                "target_hit_pct": {str(t): round(100*sum(x>=t for x in ups)/len(ups),2) if ups else None
                                   for t in (100,200,300,500)},
            }
        results[str(int(threshold) if threshold.is_integer() else threshold)] = {
            "events": len(selected), "horizons": by_horizon,
            "recent_events": [{"timestamp": bars[i][0].isoformat(), "close": closes[i], "rsi": round(rsi[i],2)}
                              for i in selected[-10:]],
        }
    return {
        "symbol": symbol, "timeframe": timeframe, "bars": len(bars),
        "first": bars[0][0].isoformat(), "last": bars[-1][0].isoformat(),
        "latest_close": closes[-1], "latest_rsi14": round(rsi[-1],2),
        "thresholds": results,
        "methodology": "Wilder RSI14; cross below threshold; non-overlapping event windows; reference entry at signal close; forward high/low and final close; no fees, slippage, fills or rollover adjustment; incomplete bars may be present; results are descriptive, not a trade simulation",
    }
