import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

from .config import DATABASE_URL, FINAM_SECRET, LOOKBACK_DAYS, POLL_SECONDS, TICKER, TIMEFRAME
from .db import Database
from .finam import FinamClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("finam-collector")

state = {
    "last_sync": None,
    "last_error": None,
    "last_received": 0,
}


def sync_db(db: Database, ticker: str, timeframe: str, bars):
    return db.upsert_bars(ticker, timeframe, bars)


async def collector_loop():
    db = Database(DATABASE_URL)
    await asyncio.to_thread(db.init_schema)
    finam = FinamClient(FINAM_SECRET)

    log.info("Collector started ticker=%s timeframe=%s poll=%ss", TICKER, TIMEFRAME, POLL_SECONDS)

    try:
        while True:
            try:
                now = datetime.now(timezone.utc)
                latest = await asyncio.to_thread(db.latest_timestamp, TICKER, TIMEFRAME)

                # First run: bootstrap history. Later runs: re-read a small overlap so
                # the currently forming candle is updated by UPSERT.
                if latest is None:
                    start = now - timedelta(days=LOOKBACK_DAYS)
                else:
                    overlap = timedelta(seconds=max(POLL_SECONDS * 4, 3600))
                    start = latest - overlap

                bars = await finam.bars(TICKER, TIMEFRAME, start, now + timedelta(seconds=1))
                written = await asyncio.to_thread(sync_db, db, TICKER, TIMEFRAME, bars)

                state["last_sync"] = now.isoformat()
                state["last_received"] = written
                state["last_error"] = None
                log.info("Synced %s bars; range=%s..%s", written, start.isoformat(), now.isoformat())
            except Exception as exc:
                state["last_error"] = repr(exc)
                log.exception("Sync failed")

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
    return {
        "status": "ok" if not state["last_error"] else "degraded",
        "ticker": TICKER,
        "timeframe": TIMEFRAME,
        "poll_seconds": POLL_SECONDS,
        **state,
    }


@app.get("/stats")
async def stats():
    db = Database(DATABASE_URL)
    count = await asyncio.to_thread(db.count, TICKER, TIMEFRAME)
    latest = await asyncio.to_thread(db.latest_timestamp, TICKER, TIMEFRAME)
    return {
        "ticker": TICKER,
        "timeframe": TIMEFRAME,
        "rows": count,
        "latest_timestamp": latest,
        **state,
    }
