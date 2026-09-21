import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .config import DATABASE_URL, FINAM_SECRET, INSTRUMENTS, POLL_SECONDS
from .dashboard import DASHBOARD_HTML
from .db import Database, INTERVALS
from .finam import FinamClient
from .moex import MoexClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("finam-collector")

BACKFILL_FROM = date.fromisoformat(os.getenv("BACKFILL_FROM", "2026-06-01"))

state = {
    "last_cycle": None,
    "instruments": {},
    "backfill": {
        "status": "pending",
        "from": BACKFILL_FROM.isoformat(),
        "current_symbol": None,
        "current_range": None,
        "written": 0,
        "last_error": None,
    },
}


def key(symbol: str, timeframe: str) -> str:
    return f"{symbol}|{timeframe}"


async def sync_one(
    db: Database,
    finam: FinamClient,
    symbol: str,
    timeframe: str,
    history_days: int,
):
    now = datetime.now(timezone.utc)
    latest = await asyncio.to_thread(db.latest_timestamp, symbol, timeframe)

    if latest is None:
        start = now - timedelta(days=history_days) + timedelta(minutes=5)
    else:
        start = latest - timedelta(hours=1)

    bars = await finam.bars(symbol, timeframe, start, now)
    written = await asyncio.to_thread(
        db.upsert_bars, symbol, timeframe, bars, "finam"
    )

    volume_present = sum(1 for b in bars if b.get("volume") is not None)
    volume_positive = 0
    for b in bars:
        v = b.get("volume")
        if isinstance(v, dict):
            v = v.get("value")
        try:
            if v is not None and float(v) > 0:
                volume_positive += 1
        except (TypeError, ValueError):
            pass

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "last_sync": now.isoformat(),
        "received": len(bars),
        "written": written,
        "payload_rows_with_volume": volume_present,
        "payload_rows_with_positive_volume": volume_positive,
        "last_error": None,
    }
    state["instruments"][key(symbol, timeframe)] = result
    log.info(
        "Synced %s %s: bars=%s volume_present=%s volume_positive=%s range=%s..%s",
        symbol,
        timeframe,
        len(bars),
        volume_present,
        volume_positive,
        start.isoformat(),
        now.isoformat(),
    )


async def collector_loop():
    db = Database(DATABASE_URL)
    finam = FinamClient(FINAM_SECRET)

    log.info(
        "Collector started instruments=%s poll=%ss",
        [f"{x.symbol}:{x.timeframe}" for x in INSTRUMENTS],
        POLL_SECONDS,
    )

    try:
        while True:
            for instrument in INSTRUMENTS:
                try:
                    await sync_one(
                        db,
                        finam,
                        instrument.symbol,
                        instrument.timeframe,
                        instrument.history_days,
                    )
                except Exception as exc:
                    k = key(instrument.symbol, instrument.timeframe)
                    previous = state["instruments"].get(k, {})
                    state["instruments"][k] = {
                        **previous,
                        "symbol": instrument.symbol,
                        "timeframe": instrument.timeframe,
                        "last_error": repr(exc),
                    }
                    log.exception(
                        "Sync failed symbol=%s timeframe=%s",
                        instrument.symbol,
                        instrument.timeframe,
                    )

            state["last_cycle"] = datetime.now(timezone.utc).isoformat()
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await finam.close()


async def backfill_loop():
    db = Database(DATABASE_URL)
    moex = MoexClient()
    state["backfill"]["status"] = "running"

    try:
        # Leave the most recent seven days to FINAM Trade API.
        finam_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date()

        for instrument in INSTRUMENTS:
            if instrument.timeframe != "TIME_FRAME_M1":
                continue

            earliest = await asyncio.to_thread(
                db.earliest_timestamp, instrument.symbol, instrument.timeframe
            )
            target_to = finam_cutoff
            if earliest is not None:
                target_to = min(target_to, (earliest - timedelta(days=1)).date())

            if target_to < BACKFILL_FROM:
                log.info("Backfill already complete for %s", instrument.symbol)
                continue

            state["backfill"]["current_symbol"] = instrument.symbol
            async for chunk_from, chunk_to, bars in moex.range_minute_bars(
                instrument.symbol,
                BACKFILL_FROM,
                target_to,
                chunk_days=7,
            ):
                state["backfill"]["current_range"] = (
                    f"{chunk_from.isoformat()}..{chunk_to.isoformat()}"
                )
                written = await asyncio.to_thread(
                    db.upsert_bars,
                    instrument.symbol,
                    "TIME_FRAME_M1",
                    bars,
                    "moex",
                )
                state["backfill"]["written"] += written
                log.info(
                    "Backfill %s %s..%s: bars=%s total_written=%s",
                    instrument.symbol,
                    chunk_from,
                    chunk_to,
                    written,
                    state["backfill"]["written"],
                )

        state["backfill"]["status"] = "complete"
        state["backfill"]["current_symbol"] = None
        state["backfill"]["current_range"] = None
        log.info("Historical backfill complete: total_written=%s", state["backfill"]["written"])
    except Exception as exc:
        state["backfill"]["status"] = "error"
        state["backfill"]["last_error"] = repr(exc)
        log.exception("Historical backfill failed")
    finally:
        await moex.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(DATABASE_URL)
    await asyncio.to_thread(db.init_schema)

    collector_task = asyncio.create_task(collector_loop())
    backfill_task = asyncio.create_task(backfill_loop())
    yield

    for task in (collector_task, backfill_task):
        task.cancel()
    for task in (collector_task, backfill_task):
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Finam Quotes Collector", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


@app.get("/health")
def health():
    errors = [v for v in state["instruments"].values() if v.get("last_error")]
    return {
        "status": "degraded" if errors else "ok",
        "poll_seconds": POLL_SECONDS,
        "configured_instruments": [
            {"symbol": x.symbol, "timeframe": x.timeframe} for x in INSTRUMENTS
        ],
        **state,
    }


@app.get("/stats")
async def stats():
    db = Database(DATABASE_URL)
    result = []
    for instrument in INSTRUMENTS:
        db_stats = await asyncio.to_thread(
            db.stats, instrument.symbol, instrument.timeframe
        )
        runtime = state["instruments"].get(
            key(instrument.symbol, instrument.timeframe), {}
        )
        result.append(
            {
                "symbol": instrument.symbol,
                "timeframe": instrument.timeframe,
                **db_stats,
                "runtime": runtime,
            }
        )
    return {
        "instruments": result,
        "last_cycle": state["last_cycle"],
        "backfill": state["backfill"],
    }


@app.get("/bars")
async def bars(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(10, ge=1, le=500),
):
    configured = {(x.symbol, x.timeframe) for x in INSTRUMENTS}
    timeframe = timeframe.upper()
    if (symbol, timeframe) not in configured:
        raise HTTPException(
            status_code=404, detail="Instrument/timeframe is not configured"
        )

    db = Database(DATABASE_URL)
    rows = await asyncio.to_thread(
        db.recent_bars, symbol, timeframe, limit
    )
    return {"symbol": symbol, "timeframe": timeframe, "bars": rows}


@app.get("/api/instruments")
def api_instruments():
    return {
        "instruments": [
            {"symbol": x.symbol, "timeframe": x.timeframe}
            for x in INSTRUMENTS
        ],
        "intervals": list(INTERVALS.keys()),
    }


@app.get("/api/chart")
async def api_chart(
    symbol: str = Query(...),
    interval: str = Query("5m"),
    days: int = Query(7, ge=0, le=365),
):
    configured_symbols = {x.symbol for x in INSTRUMENTS}
    if symbol not in configured_symbols:
        raise HTTPException(status_code=404, detail="Instrument is not configured")
    if interval not in INTERVALS:
        raise HTTPException(status_code=400, detail="Unsupported interval")

    now = datetime.now(timezone.utc)
    if days == 0:
        date_from = datetime.combine(
            BACKFILL_FROM, datetime.min.time(), tzinfo=timezone.utc
        )
    else:
        date_from = now - timedelta(days=days)

    db = Database(DATABASE_URL)
    rows = await asyncio.to_thread(
        db.chart_bars, symbol, interval, date_from, now
    )

    return {
        "symbol": symbol,
        "interval": interval,
        "days": days,
        "bars": rows,
        "source_note": "MOEX history + FINAM live",
        "backfill": state["backfill"],
    }


@app.get("/api/backfill")
def api_backfill():
    return state["backfill"]
