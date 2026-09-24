import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .config import DATABASE_URL, FINAM_SECRET, INSTRUMENTS, POLL_SECONDS
from .db import Database, INTERVALS
from .finam import FinamClient
from .moex import MoexClient
from .analysis import build_si_analysis, build_technical_snapshot, build_si_extended_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("finam-collector")

BACKFILL_FROM = os.getenv("BACKFILL_FROM", "2026-06-01").strip()
BACKFILL_ENABLED = os.getenv("BACKFILL_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
BACKFILL_CONCURRENCY = max(1, min(int(os.getenv("BACKFILL_CONCURRENCY", "3")), 5))

state = {
    "last_cycle": None,
    "instruments": {},
    "backfill": {
        "enabled": BACKFILL_ENABLED,
        "from": BACKFILL_FROM,
        "status": "pending" if BACKFILL_ENABLED else "disabled",
        "current": None,
        "days_done": 0,
        "rows_written": 0,
        "last_error": None,
        "instruments": {},
    },
}


FINAM_LIVE_SYMBOLS = {
    "BR@CONT": "BRV6@RTSX",
    "SI@CONT": "SiZ6@RTSX",
}


def key(symbol: str, timeframe: str) -> str:
    return f"{symbol}|{timeframe}"


async def sync_one(db: Database, finam: FinamClient, symbol: str, timeframe: str, history_days: int):
    now = datetime.now(timezone.utc)
    latest = await asyncio.to_thread(db.latest_timestamp, symbol, timeframe)

    max_start = now - timedelta(days=history_days) + timedelta(minutes=5)
    if latest is None:
        start = max_start
    else:
        start = max(latest - timedelta(hours=1), max_start)

    finam_symbol = FINAM_LIVE_SYMBOLS.get(symbol, symbol)
    bars = await finam.bars(finam_symbol, timeframe, start, now)
    written = await asyncio.to_thread(db.upsert_bars, symbol, timeframe, bars, "finam")

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
        "finam_symbol": finam_symbol,
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
        "Synced %s via %s %s: bars=%s volume_present=%s volume_positive=%s",
        symbol, finam_symbol, timeframe, len(bars), volume_present, volume_positive,
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
                        db, finam, instrument.symbol, instrument.timeframe, instrument.history_days
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


async def backfill_instrument(
    db: Database,
    moex: MoexClient,
    instrument,
    start_date,
    cutoff,
    semaphore: asyncio.Semaphore,
) -> bool:
    symbol = instrument.symbol
    progress = {
        "status": "running",
        "current": None,
        "days_done": 0,
        "rows_written": 0,
        "last_error": None,
    }
    state["backfill"]["instruments"][symbol] = progress

    completed = await asyncio.to_thread(
        db.ensure_backfill_window, symbol, start_date
    )
    day = max(start_date, completed + timedelta(days=1))

    try:
        while day <= cutoff:
            progress["current"] = day.isoformat()

            active = [
                f"{name} {item.get('current')}"
                for name, item in state["backfill"]["instruments"].items()
                if item.get("status") == "running" and item.get("current")
            ]
            state["backfill"]["current"] = " | ".join(active[:4])

            async with semaphore:
                bars = await moex.minute_bars(symbol, day)

            if bars:
                written = await asyncio.to_thread(
                    db.upsert_bars,
                    symbol,
                    instrument.timeframe,
                    bars,
                    "moex",
                )
                progress["rows_written"] += written
                state["backfill"]["rows_written"] += written

            # Checkpoint every successfully processed calendar day, including weekends.
            await asyncio.to_thread(
                db.mark_backfill_completed, symbol, start_date, day
            )
            progress["days_done"] += 1
            state["backfill"]["days_done"] += 1
            progress["last_error"] = None
            day += timedelta(days=1)
            await asyncio.sleep(0.03)

        progress["status"] = "done"
        progress["current"] = None
        log.info(
            "Backfill completed symbol=%s rows_written=%s",
            symbol,
            progress["rows_written"],
        )
        return True
    except Exception as exc:
        progress["status"] = "error"
        progress["last_error"] = repr(exc)
        state["backfill"]["last_error"] = f"{symbol}: {exc!r}"
        log.exception("Backfill failed symbol=%s day=%s", symbol, day)
        return False


async def backfill_loop():
    if not BACKFILL_ENABLED:
        return

    db = Database(DATABASE_URL)
    moex = MoexClient()
    try:
        start_date = datetime.fromisoformat(BACKFILL_FROM).date()
        # FINAM owns the most recent 7 days; MOEX is used only for the deep-history gap.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date()
        state["backfill"]["status"] = "running"
        state["backfill"]["instruments"] = {}
        semaphore = asyncio.Semaphore(BACKFILL_CONCURRENCY)

        instruments = [
            instrument
            for instrument in INSTRUMENTS
            if instrument.timeframe == "TIME_FRAME_M1"
        ]

        results = await asyncio.gather(
            *[
                backfill_instrument(
                    db,
                    moex,
                    instrument,
                    start_date,
                    cutoff,
                    semaphore,
                )
                for instrument in instruments
            ],
            return_exceptions=False,
        )

        state["backfill"]["current"] = None
        state["backfill"]["status"] = "done" if all(results) else "partial_error"
        log.info(
            "Backfill finished status=%s rows_written=%s concurrency=%s",
            state["backfill"]["status"],
            state["backfill"]["rows_written"],
            BACKFILL_CONCURRENCY,
        )
    finally:
        await moex.close()


async def log_gmkn_snapshot_once():
    await asyncio.sleep(6)
    try:
        db = Database(DATABASE_URL)
        snapshot = await asyncio.to_thread(build_technical_snapshot, db, "GMKN@MISX")
        log.info("GMKN_TECH_SNAPSHOT %s", json.dumps(snapshot, ensure_ascii=False, default=str))
    except Exception:
        log.exception("GMKN technical snapshot failed")


async def log_si_snapshot_once():
    await asyncio.sleep(2)
    try:
        db = Database(DATABASE_URL)
        snapshot = await asyncio.to_thread(build_si_extended_snapshot, db, "SI@CONT")
        log.info("SI_EXTENDED_SNAPSHOT %s", json.dumps(snapshot, ensure_ascii=False, default=str))
    except Exception:
        log.exception("SI extended snapshot failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(DATABASE_URL)
    await asyncio.to_thread(db.init_schema)
    collector = asyncio.create_task(collector_loop())
    backfill = asyncio.create_task(backfill_loop())
    gmkn_snapshot = asyncio.create_task(log_gmkn_snapshot_once())
    si_snapshot = asyncio.create_task(log_si_snapshot_once())
    yield
    for task in (collector, backfill, gmkn_snapshot, si_snapshot):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Finam Quotes Collector", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


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
    return {"instruments": result, "last_cycle": state["last_cycle"], "backfill": state["backfill"]}


@app.get("/backfill/status")
def backfill_status():
    return state["backfill"]


@app.get("/api/instruments")
def instruments():
    return {
        "instruments": [
            {"symbol": x.symbol, "timeframe": x.timeframe} for x in INSTRUMENTS
        ]
    }


def parse_iso(value: str | None):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@app.get("/api/candles")
async def api_candles(
    symbol: str = Query(...),
    timeframe: str = Query("TIME_FRAME_M1"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    max_points: int = Query(5000, ge=100, le=10000),
    interval: str = Query("1m"),
):
    configured = {(x.symbol, x.timeframe) for x in INSTRUMENTS}
    timeframe = timeframe.upper()
    if (symbol, timeframe) not in configured:
        raise HTTPException(status_code=404, detail="Instrument/timeframe is not configured")

    if interval not in INTERVALS:
        raise HTTPException(status_code=400, detail="Unsupported interval")

    db = Database(DATABASE_URL)
    rows = await asyncio.to_thread(
        db.chart_bars,
        symbol,
        timeframe,
        parse_iso(start),
        parse_iso(end),
        max_points,
        interval,
    )
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "interval": interval,
        "bars": rows,
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
        raise HTTPException(status_code=404, detail="Instrument/timeframe is not configured")

    db = Database(DATABASE_URL)
    rows = await asyncio.to_thread(db.recent_bars, symbol, timeframe, limit)
    return {"symbol": symbol, "timeframe": timeframe, "bars": rows}


@app.get("/api/analysis/si")
async def si_analysis():
    db = Database(DATABASE_URL)
    return await asyncio.to_thread(build_si_analysis, db, "SI@CONT")
