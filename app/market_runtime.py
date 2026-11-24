import asyncio
import logging
import os
import re
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from .config import INSTRUMENTS
from .market_calendar import sessions
from .market_ingest import MarketIngest, timestamp

log = logging.getLogger(__name__)
SI_CONTRACT = re.compile(r"Si[HMUZ][0-9]@RTSX", re.IGNORECASE)


async def select_contracts(finam, now):
    assets = await finam._get("/v1/assets")
    candidates = {
        asset["symbol"]
        for asset in assets["assets"]
        if not asset.get("is_archived") and SI_CONTRACT.fullmatch(asset.get("symbol", ""))
    }
    contracts = []
    for symbol in sorted(candidates)[:24]:
        try:
            asset = await finam._get(f"/v1/assets/{symbol}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue
            raise
        expires = asset.get("future_details", {}).get("expiration_date")
        if expires and now < timestamp(expires):
            contracts.append((timestamp(expires), symbol))
    return sorted(contracts)


async def runtime(app, store, finam, mode, sync_one, db, live_symbols):
    worker = None
    current = None
    expires = None
    last_lookup = None
    extra_symbols = []
    symbol_override = os.getenv("MARKET_V2_SYMBOL", "").strip()
    next_contract_candles = os.getenv("MARKET_V2_NEXT_CONTRACT_CANDLES", "false") == "true"
    book_seconds = max(1, int(os.getenv("MARKET_V2_BOOK_SECONDS", "5")))
    trade_seconds = max(15, int(os.getenv("MARKET_V2_TRADES_SECONDS", "30")))
    quote_seconds = max(15, int(os.getenv("MARKET_V2_QUOTE_SECONDS", "15")))
    configured = {i.symbol for i in INSTRUMENTS if i.timeframe == "TIME_FRAME_M1"}
    try:
        while True:
            now = datetime.now(UTC)
            app.state.market_sessions = await asyncio.to_thread(sessions)
            if (
                last_lookup is None
                or (now - last_lookup).total_seconds() >= 900
                or (expires is not None and expires <= now)
            ):
                last_lookup = now
                try:
                    contracts = await select_contracts(finam, now)
                    app.state.market_contracts = [symbol for _, symbol in contracts]
                    expires, app.state.market_active_symbol = (
                        contracts[0] if contracts else (None, None)
                    )
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    log.warning("Finam contract lookup failed: %s", type(exc).__name__)
                    if expires is None or expires <= now:
                        expires = None
                        app.state.market_active_symbol, app.state.market_contracts = None, []
            active = app.state.market_active_symbol
            if active:
                live_symbols["SI@CONT"] = active
            # Repository legacy fallback stays configured when contract discovery has no record.
            symbol = symbol_override or active
            if symbol != current or (worker is not None and worker.done()):
                if worker is not None:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        log.warning("Market worker restart: %s", type(exc).__name__)
                current = symbol
                worker = (
                    asyncio.create_task(
                        MarketIngest(
                            store, finam, symbol, book_seconds, trade_seconds, quote_seconds
                        ).run(mode)
                    )
                    if symbol
                    else None
                )
            extra_symbols = [symbol] if symbol and symbol not in configured else []
            if next_contract_candles and len(app.state.market_contracts) > 1:
                following = app.state.market_contracts[1]
                if following not in configured and following not in extra_symbols:
                    extra_symbols.append(following)
            for item in extra_symbols:
                try:
                    await sync_one(db, finam, item, "TIME_FRAME_M1", 7)
                except Exception as exc:
                    log.warning(
                        "Market contract candle sync failed symbol=%s error=%s",
                        item,
                        type(exc).__name__,
                    )
            await asyncio.sleep(60)
    finally:
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
