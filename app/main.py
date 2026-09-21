import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query

from .config import DATABASE_URL, FINAM_SECRET, INSTRUMENTS, POLL_SECONDS
from .db import Database
from .finam import FinamClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("finam-collector")

state = {
    "last_cycle": None,
    "instruments": {},
}


def key(symbol: str, timeframe: str) -> str:
    return f"{symbol}|{timeframe}"


async def sync_one(db: Database, finam: FinamClient, symbol: str, timeframe: str, history_days: int):
    now = datetime.now(timezone.utc)
    latest = await asyncio.to_thread(db.latest_timestamp, symbol, timeframe)

    if latest is None:
        # Stay slightly inside Finam's maximum history window.\n        # Asking for exactly the documented limit can become invalid by the time\n        # the request is processed, especially for M1 (7 days).\n        start = now - timedelta(days=history_days) + timedelta(minutes=5)\n    else:
        # Re-read overlap to keep the currently-forming candle and recent bars fresh.
        start = latest - timedelta(hours=1)

    bars = await finam.bars(symbol, timeframe, start, now)\n    written = await asyncio.to_thread(db.upsert_bars, symbol, timeframe, bars)

    # Explicitly inspect the payload for volume; this catches API/schema surprises early.
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
    await asyncio.to_thread(db.init_schema)
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
                    log.exception("Sync failed symbol=%s timeframe=%s", instrument.symbol, instrument.timeframe)

            state["last_cycle"] = datetime.now(timezone.utc).isoformat()
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await finam.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(collector_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Finam Quotes Collector", lifespan=lifespan)


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
        db_stats = await asyncio.to_thread(db.stats, instrument.symbol, instrument.timeframe)
        runtime = state["instruments"].get(key(instrument.symbol, instrument.timeframe), {})
        result.append({
            "symbol": instrument.symbol,
            "timeframe": instrument.timeframe,
            **db_stats,
            "runtime": runtime,
        })
    return {"instruments": result, "last_cycle": state["last_cycle"]}


@app.get("/bars")
async def bars(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(10, ge=1, le=500),
):
    configured = {(x.symbol, x.timeframe) for x in INSTRUMENTS}
    timeframe = timeframe.upper()
    if (symbol, timeframe) not in configured:
        raise HTTPException(status_code=404, detail="Instrument/timeframe is not configured")

    db = Database(DATABASE_URL)
    rows = await asyncio.to_thread(db.recent_bars, symbol, timeframe, limit)
    return {"symbol": symbol, "timeframe": timeframe, "bars": rows}
