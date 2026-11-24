from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import resource
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from marketdata.contracts import (
    FRAME_SECONDS,
    BookPageQuery,
    BookQuery,
    CrossQuery,
    FlowQuery,
    FrameQuery,
    MarketQuery,
    PageQuery,
    ResearchQuery,
    SnapshotQuery,
)

from . import market_metrics as metrics
from .config import DATABASE_URL, INSTRUMENTS
from .market_calendar import session_context
from .market_research import REQUIRES_MICROSTRUCTURE, research
from .market_store import (
    FLOW_WINDOW_LIMIT,
    MarketStore,
    execute,
    load_bars,
    load_book,
    load_flow,
    load_latest_minute,
    load_market_features,
    load_oi,
    load_quote,
    load_trades,
)
from .research_api import authorize

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", dependencies=[Depends(authorize)], tags=["market-v2"])
request_times = deque()
request_lock = Lock()
read_slots = BoundedSemaphore(8)
research_lock = Lock()
research_cache = OrderedDict()
RESEARCH_CACHE_SECONDS = 30
PROCESS_SAMPLE_START = time.monotonic()
PROCESS_CPU_START = time.process_time()


def rate_limit(request: Request):
    if len(request.scope["query_string"]) > 4096:
        raise HTTPException(414, "Market v2 query is too long")
    with request_lock:
        now = time.monotonic()
        while request_times and request_times[0] <= now - 60:
            request_times.popleft()
        if len(request_times) >= 120:
            raise HTTPException(429, "Market v2 rate limit", headers={"Retry-After": "60"})
        request_times.append(now)


def response(
    symbol, now, max_age, session, data=None, *, ts=None, source=None, quality=None, warnings=()
):
    age = (now - ts).total_seconds() if ts else None
    fresh = age is not None and 0 <= age <= max_age
    if quality is None:
        quality = (
            "UNAVAILABLE"
            if data is None
            else "MARKET_CLOSED"
            if session == "MARKET_CLOSED" and not fresh
            else "HEALTHY"
            if fresh
            else "DATA_STALE"
            if ts
            else "PARTIAL_DATA"
        )
    return {
        "instrument": symbol,
        "source": source,
        "exchange_timestamp": ts,
        "server_timestamp": now,
        "data_age_seconds": age,
        "is_fresh": fresh,
        "data_quality_status": quality,
        "market_session_status": session,
        "warnings": list(warnings),
        "schema_version": "2.0",
        "data": data,
    }


def encode_cursor(as_of, timestamp, key):
    payload = json.dumps([as_of.isoformat(), timestamp.isoformat(), key], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor, now, *, integer):
    if cursor is None:
        return now, None
    try:
        as_of, timestamp, key = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        as_of, timestamp = datetime.fromisoformat(as_of), datetime.fromisoformat(timestamp)
        if not now - timedelta(days=1) <= as_of <= now or timestamp > as_of:
            raise ValueError
        if not (
            type(key) is int and 0 < key < 2**63
            if integer
            else isinstance(key, str) and 0 < len(key) <= 128
        ):
            raise ValueError
        return as_of, (timestamp, key)
    except (ValueError, TypeError, binascii.Error, OverflowError):
        raise HTTPException(422, "Invalid or expired cursor") from None


def load_technical(conn, symbol, frames, as_of, session):
    session_bars, previous_bars, opening = [], [], []
    interval_sets = session["intervals"] + session["previous_day_intervals"]
    if interval_sets:
        start = min(s for s, _ in interval_sets)
        raw = load_bars(
            conn,
            symbol,
            "1m",
            as_of,
            limit=3000,
            window=max(60, int((as_of - start).total_seconds()) + 60),
        )

        def in_windows(bar, windows):
            return any(s <= bar["exchange_ts"] < e for s, e in windows)

        session_bars = [b for b in raw if in_windows(b, session["intervals"])]
        previous_bars = [b for b in raw if in_windows(b, session["previous_day_intervals"])]
        first = session["session_start"]
        if first and as_of >= first + timedelta(minutes=15):
            opening = [b for b in session_bars if b["exchange_ts"] < first + timedelta(minutes=15)]
    result = {}
    for frame in dict.fromkeys(frames):
        bars = load_bars(conn, symbol, frame, as_of)
        result[frame] = (
            metrics.indicators(bars, session_bars, previous_bars, opening) if bars else None
        )
        if bars:
            result[frame]["source"] = bars[-1]["source"]
    return result


def counter_status(conn, symbol):
    health = execute(
        conn,
        "SELECT feed,status,updated_at,detail FROM ingestion_health WHERE symbol=%s",
        (symbol,),
    )
    size = execute(conn, "SELECT pg_database_size(current_database()) AS size")[0]["size"]
    disk = os.statvfs(Path(os.getenv("MARKET_V2_DISK_PATH", "/")))
    load = os.getloadavg()
    memory_kib = None
    try:
        proc = Path("/proc/self/status").read_text()
        memory_kib = int(
            next(line for line in proc.splitlines() if line.startswith("VmRSS:")).split()[1]
        )
    except (OSError, StopIteration, ValueError):
        pass
    return {
        "feeds": health,
        "database_size_bytes": size,
        "collector_filesystem_used_bytes": (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
        "collector_filesystem_available_bytes": disk.f_bavail * disk.f_frsize,
        "host_load_average": load,
        "collector_rss_kib": memory_kib,
        "collector_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "collector_average_cpu_percent_of_one_core": 100
        * max(0, time.process_time() - PROCESS_CPU_START)
        / max(0.001, time.monotonic() - PROCESS_SAMPLE_START),
    }


class MarketRead:
    def __init__(self, conn, cutoff, query, app):
        self.conn, self.cutoff, self.query = conn, cutoff, query
        self.request_symbol = query.symbol
        self.active = getattr(app.state, "market_active_symbol", None)
        self.catalog = getattr(app.state, "market_contracts", [])
        self.symbol = self.active if query.symbol == "SI@CONT" and self.active else query.symbol
        self.candle_symbol = self.symbol
        self.ingest_enabled = getattr(app.state, "market_mode", "off") != "off"
        self.calendar = getattr(app.state, "market_sessions", {})
        session_symbol = self.symbol if self.symbol != "SI@CONT" else "Si@RTSX"
        self.session = session_context(session_symbol, cutoff, self.calendar)
        self.configured = {item.symbol for item in INSTRUMENTS} | set(self.catalog) | {"SI@CONT"}
        if self.active:
            self.configured.add(self.active)
        additional = execute(conn, "SELECT symbol FROM market_instruments LIMIT 1000")
        self.configured.update(row["symbol"] for row in additional)
        if self.symbol not in self.configured:
            raise HTTPException(404, "Instrument is not configured")

    def wrap(self, data=None, *, row=None, ts=None, source=None, quality=None, warnings=()):
        if row:
            ts, source = row.get("exchange_ts"), row.get("source")
            if ts and row.get("received_ts"):
                data = (
                    dict(data, ingestion_lag_seconds=(row["received_ts"] - ts).total_seconds())
                    if isinstance(data, dict)
                    else data
                )
        return response(
            self.symbol,
            self.cutoff,
            self.query.max_age_seconds,
            self.session["status"],
            data,
            ts=ts,
            source=source,
            quality=quality,
            warnings=warnings,
        )

    def capabilities(self):
        rows = execute(
            self.conn,
            """SELECT
                 EXISTS(SELECT 1 FROM orderbook_snapshots WHERE symbol=%s) AS orderbook,
                 EXISTS(SELECT 1 FROM market_trades WHERE symbol=%s) AS trades,
                 EXISTS(SELECT 1 FROM open_interest_history WHERE symbol=%s) AS open_interest,
                 EXISTS(SELECT 1 FROM market_quotes WHERE symbol=%s) AS quote,
                 EXISTS(SELECT 1 FROM candles WHERE ticker=%s) AS candles""",
            (self.symbol,) * 5,
        )[0]
        return self.wrap(
            {
                "observed": rows,
                "ingestion_enabled": self.ingest_enabled,
                "requested_symbol": self.request_symbol,
                "resolved_si_contract": self.active,
                "front_contracts": self.catalog,
                "source_entitlements": "UNVERIFIED",
                "book_sequence": "UNAVAILABLE",
                "book_orders_count": "UNAVAILABLE",
                "trade_replay_completeness": "UNVERIFIED",
            },
            quality="HEALTHY",
        )

    def quote(self):
        raw = load_quote(self.conn, self.symbol, self.cutoff)
        if raw and raw[0]["quote"].get("last") is not None:
            row = raw[0]
            return self.wrap(dict(row["quote"], price_semantics="Finam last trade"), row=row)
        bars = load_latest_minute(self.conn, self.candle_symbol, self.cutoff)
        observation_symbol = self.candle_symbol
        if not bars and self.request_symbol == "SI@CONT" and self.candle_symbol != "SI@CONT":
            observation_symbol = "SI@CONT"
            bars = load_latest_minute(self.conn, observation_symbol, self.cutoff)
        if not bars:
            return self.wrap(warnings=["No quote or observed minute candle."])
        row = bars[-1]
        provisional = row["exchange_ts"] + timedelta(minutes=1) > self.cutoff
        return self.wrap(
            {
                "last": row["close"],
                "provisional": provisional,
                "price_semantics": "latest stored 1m candle close",
                "observation_symbol": observation_symbol,
            },
            row=row,
            warnings=["Minute-candle fallback. Timestamp is the candle open."],
        )

    def page(self, feed, *, min_quantity=None):
        q = self.query
        cutoff, cursor = decode_cursor(q.cursor, self.cutoff, integer=feed == "book")
        start = cutoff - timedelta(seconds=q.history_window)
        load = {"book": load_book, "trades": load_trades, "oi": load_oi}[feed]
        if min_quantity is not None:
            rows = load(
                self.conn,
                self.symbol,
                start,
                cutoff,
                q.limit + 1,
                cursor,
                min_quantity=min_quantity,
            )
        else:
            rows = load(self.conn, self.symbol, start, cutoff, q.limit + 1, cursor)
        has_more = len(rows) > q.limit
        rows = rows[: q.limit]
        if not rows:
            return self.wrap(warnings=["No observed rows in the requested time window."])
        time_key, id_key = {
            "book": ("received_ts", "id"),
            "trades": ("exchange_ts", "trade_id"),
            "oi": ("exchange_ts", "source"),
        }[feed]
        after = encode_cursor(cutoff, rows[-1][time_key], rows[-1][id_key]) if has_more else None
        records = (
            [metrics.book_metrics(r, q.orderbook_depth) for r in rows] if feed == "book" else rows
        )
        return self.wrap(
            {
                "rows": records,
                "as_of": cutoff,
                "next_cursor": after,
                "selection_min_quantity": min_quantity,
            },
            row=rows[0],
            quality="PARTIAL_DATA" if feed == "trades" else None,
            warnings=["Coverage between observed trades is unverified."]
            if feed == "trades"
            else [],
        )

    def book(self, kind):
        rows = load_book(
            self.conn,
            self.symbol,
            self.cutoff - timedelta(days=31),
            self.cutoff,
            limit=2 if kind != "book" else 1,
        )
        if not rows:
            return self.wrap(warnings=["No stored Level 2 snapshot."])
        books = [metrics.book_metrics(row, self.query.orderbook_depth) for row in rows]
        latest = books[0]
        data = latest
        warning = ["Provider sequence and orders count are unavailable."]
        if kind == "imbalance":
            data = {
                key: latest[key]
                for key in ("imbalance", "imbalance_by_depth", "bid_volume", "ask_volume", "depth")
            }
        if kind in {"liquidity", "anomalies"}:
            data = metrics.liquidity(latest, books[1] if len(books) > 1 else None)
        if kind == "anomalies":
            removed, added = data["removed_at_shared_prices"], data["added_at_shared_prices"]
            data = {
                "liquidity_anomaly": bool(removed > max(added, 0) * 3 and removed >= 100)
                if removed is not None
                else None,
                "removed_at_shared_prices": removed,
                "method": "Heuristic: at least 100 contracts and 3x additions at shared levels.",
            }
        stream = execute(
            self.conn,
            """SELECT status FROM ingestion_health
                                       WHERE symbol=%s AND feed='stream'""",
            (self.symbol,),
        )
        quality = None
        if (
            stream
            and stream[0]["status"] == "STREAM_DISCONNECTED"
            and self.session["status"] != "MARKET_CLOSED"
        ):
            quality = "STREAM_DISCONNECTED"
        if latest["quality"] != "HEALTHY":
            quality = quality or "PARTIAL_DATA"
        return self.wrap(data, row=latest, quality=quality, warnings=warning)

    def oi(self):
        rows = load_oi(self.conn, self.symbol, self.cutoff - timedelta(days=2), self.cutoff, 5000)
        if not rows:
            return self.wrap(
                warnings=["Finam has supplied no open-interest field for this symbol."]
            )
        current = rows[0]
        symbol = self.symbol
        start = self.session["session_start"]
        data = metrics.oi_metrics(rows)
        same = [r for r in rows if r["source"] == current["source"]]
        first = min(
            (r for r in same if start and start <= r["exchange_ts"]),
            default=None,
            key=lambda r: r["exchange_ts"],
        )
        data["session_change"] = (
            current["open_interest"] - first["open_interest"]
            if first and first != current
            else None
        )
        for changes in data["changes_minutes"].values():
            base_ts = changes["baseline_timestamp"]
            oi_change = changes["absolute"]
            if base_ts is None or oi_change is None:
                continue
            a = load_bars(self.conn, symbol, "1m", base_ts, limit=1)
            b = load_bars(self.conn, symbol, "1m", current["exchange_ts"], limit=1)
            if not (a and b) or a[-1]["exchange_ts"] == b[-1]["exchange_ts"]:
                continue
            if base_ts - a[-1]["exchange_ts"] > timedelta(minutes=2) or current["exchange_ts"] - b[
                -1
            ]["exchange_ts"] > timedelta(minutes=2):
                continue
            price_change = b[-1]["close"] - a[-1]["close"]

            def direction(value):
                return "UP" if value > 0 else "DOWN" if value < 0 else "FLAT"

            changes["price_oi_case"] = f"PRICE_{direction(price_change)}_OI_{direction(oi_change)}"
            changes["price_change_points"] = price_change
        data["interpretation"] = (
            "UP/UP: participation expanding during a rise; DOWN/UP: expanding "
            "during a decline; UP/DOWN: contracting during a rise; DOWN/DOWN: "
            "contracting during a decline. These are descriptive hypotheses."
        )
        return self.wrap(
            data, row=current, warnings=["Observed spacing can differ from provider cadence."]
        )

    def trade_analytics(self, kind, *, window=None, interval=None, rows=None):
        window = self.query.history_window if window is None else window
        interval = interval or getattr(self.query, "interval", 60)
        if kind in {"cvd", "volume-delta"}:
            return self.trade_flow_series(kind, window, interval)
        if rows is None:
            rows = load_trades(
                self.conn, self.symbol, self.cutoff - timedelta(seconds=window), self.cutoff
            )
        if not rows:
            return self.wrap(warnings=["No market trades observed in the requested window."])
        if len(rows) > FLOW_WINDOW_LIMIT:
            return self.wrap(
                warnings=["Trade window exceeds row budget. Request a shorter window."]
            )
        if kind == "volume-profile":
            data = metrics.volume_profile(rows)
        else:
            data = {
                "observed_count": len(rows),
                "latest": rows[0],
                "largest_observed_trades": sorted(rows, key=lambda r: r["quantity"], reverse=True)[
                    :5
                ],
            }
        return self.wrap(
            data,
            row=rows[0],
            quality="PARTIAL_DATA",
            warnings=[
                "Coverage and aggressor semantics are unverified. "
                "CVD starts at the observed window."
            ],
        )

    def trade_flow_series(self, kind, window, interval):
        # Align the oldest instant to a full UTC bucket. State the actual cutoff.
        end_seconds = int(self.cutoff.timestamp())
        start = datetime.fromtimestamp((end_seconds - window) // interval * interval, UTC)
        rows = load_flow(self.conn, self.symbol, start, self.cutoff, interval)
        if not rows:
            return self.wrap(warnings=["No observed trade aggregates for the requested window."])
        buckets = metrics.flow_buckets(rows, interval, self.cutoff)
        key = "cvd_observed_window" if kind == "cvd" else "volume_delta"
        data = {
            "interval_seconds": interval,
            "window_start": start,
            "point_count": len(buckets),
            "latest": buckets[-1][key],
            "buckets": buckets[-120:],
        }
        return self.wrap(
            data,
            ts=rows[-1]["exchange_ts"],
            source="finam",
            quality="PARTIAL_DATA",
            warnings=[
                "Coverage and aggressor semantics are unverified. "
                "CVD starts at the first observed bucket in this window."
            ],
        )

    def technical(self):
        data = load_technical(
            self.conn, self.candle_symbol, self.query.timeframes, self.cutoff, self.session
        )
        present = [v for v in data.values() if v]
        if not present:
            return self.wrap(warnings=["No completed candles for the requested timeframes."])
        latest = max(present, key=lambda entry: entry["exchange_ts"])
        return self.wrap(
            data,
            ts=latest["exchange_ts"],
            source=latest["source"],
            quality="PARTIAL_DATA" if len(present) < len(data) else None,
            warnings=[
                "VWAP uses OHLCV typical prices. UTC buckets may include intrabucket market breaks."
            ],
        )

    def statistics(self, query=None, *, cache=True):
        q = query or self.query
        if q.symbol == "SI@CONT":
            return self.wrap(
                warnings=["Select an explicit contract with stored candles for research."]
            )
        cache_key = (self.symbol, q.timeframe, q.history_window, tuple(sorted(q.conditions)))
        with research_lock:
            cached = research_cache.get(cache_key)
            if (
                cache
                and cached
                and 0 <= (self.cutoff - cached[0]).total_seconds() <= RESEARCH_CACHE_SECONDS
            ):
                research_cache.move_to_end(cache_key)
                _, data, last = cached
                return self.research_response(data, last, q.conditions, cached_at=cached[0])
        bars = load_bars(
            self.conn, self.symbol, q.timeframe, self.cutoff, limit=10000, window=q.history_window
        )
        if len(bars) < 152:
            return self.wrap(
                quality="INSUFFICIENT_HISTORY", warnings=["Requires 152 completed bars."]
            )
        microstructure = REQUIRES_MICROSTRUCTURE.intersection(q.conditions)
        # Early rollouts can accumulate different history depths per feed.
        observations = (
            load_market_features(
                self.conn,
                self.symbol,
                q.timeframe,
                bars[0]["exchange_ts"],
                self.cutoff,
                need_book="book_imbalance" in microstructure,
                need_oi=bool({"oi_anomaly", "price_oi_divergence"} & microstructure),
                need_cvd="price_cvd_divergence" in microstructure,
            )
            if microstructure
            else {}
        )
        data = research(bars, q.conditions, FRAME_SECONDS[q.timeframe], observations)
        with research_lock:
            if cache_key not in research_cache or research_cache[cache_key][0] < self.cutoff:
                research_cache[cache_key] = (self.cutoff, data, bars[-1])
                research_cache.move_to_end(cache_key)
            while len(research_cache) > 16:
                research_cache.popitem(last=False)
        return self.research_response(data, bars[-1], q.conditions, cached_at=self.cutoff)

    def research_response(self, data, last, conditions, *, cached_at):
        microstructure = REQUIRES_MICROSTRUCTURE.intersection(conditions)
        return self.wrap(
            dict(data, research_as_of=cached_at),
            row=last,
            quality=(
                "INSUFFICIENT_HISTORY"
                if not any(h["independent_windows"] for h in data["horizons"].values())
                else "PARTIAL_DATA"
                if microstructure
                else "HEALTHY"
            ),
            warnings=["Research frequency, with no execution or transaction costs."]
            + (
                ["Microstructure inputs use observed events and may have gaps."]
                if microstructure
                else []
            ),
        )

    def cross_market(self, query=None):
        q = query or self.query
        comparison = q.comparison_symbol or (self.catalog[1] if len(self.catalog) > 1 else None)
        if not comparison or comparison == self.symbol or comparison not in self.configured:
            return self.wrap(warnings=["Choose another configured instrument with stored candles."])
        left, right = [
            load_bars(self.conn, s, q.timeframe, self.cutoff, limit=3000, window=q.history_window)
            for s in (self.symbol, comparison)
        ]
        data = metrics.cross_market(left, right, FRAME_SECONDS[q.timeframe])
        if not data:
            return self.wrap(
                quality="INSUFFICIENT_HISTORY", warnings=["No aligned consecutive returns."]
            )
        data["comparison_symbol"] = comparison
        if self.symbol in self.catalog and comparison in self.catalog:
            data["calendar_spread_left_minus_right"] = data["left_close"] - data["right_close"]
        data["basis"] = None
        return self.wrap(
            data,
            ts=data["timestamp"],
            source="stored_ohlcv",
            warnings=[
                "Exact match by UTC bucket. No price forward-fill across sessions. "
                "Futures basis requires a verified spot instrument and price units."
            ],
        )

    def health(self, current=None):
        data = counter_status(self.conn, self.symbol)
        if current is None:
            current = self.quote()
        statuses = {row["status"] for row in data["feeds"]}
        bad = {"API_UNAVAILABLE", "RATE_LIMITED", "STREAM_DISCONNECTED"}
        stale = [
            row["feed"]
            for row in data["feeds"]
            if (self.cutoff - row["updated_at"]).total_seconds() > self.query.max_age_seconds
        ]
        quality = (
            "DEGRADED"
            if statuses & bad or (stale and self.session["status"] != "MARKET_CLOSED")
            else "MARKET_CLOSED"
            if self.session["status"] == "MARKET_CLOSED"
            else "PARTIAL_DATA"
            if "PARTIAL_DATA" in statuses and current["is_fresh"]
            else current["data_quality_status"]
        )
        return self.wrap(
            dict(
                data,
                stale_feeds=stale,
                collector_api_ready=True,
                data_ingest_enabled=self.ingest_enabled,
            ),
            ts=current["exchange_timestamp"],
            source=current["source"],
            quality=quality,
        )

    def snapshot(self):
        q = self.query
        book = self.book("book")
        technical = self.technical()
        quote = self.quote()
        latest = quote["data"]
        rows = load_trades(
            self.conn, self.symbol, self.cutoff - timedelta(seconds=q.history_window), self.cutoff
        )
        blocks = {
            "quote": quote,
            "market_session": self.session,
            "data_quality": self.health(quote),
            "candles_summary": self.wrap(
                {
                    f: {"timestamp": r["exchange_ts"], "candles": r["candles_used"]}
                    for f, r in (technical["data"] or {}).items()
                    if r
                },
                ts=technical["exchange_timestamp"],
                source=technical["source"],
            )
            if technical["data"]
            else self.wrap(),
            "price_action": self.wrap(
                {"last": latest["last"]}, ts=quote["exchange_timestamp"], source=quote["source"]
            )
            if latest
            else self.wrap(),
            "technical_indicators": technical,
            "orderbook": book,
            "liquidity": self.book("liquidity"),
            "trade_flow": self.trade_analytics("trade-flow", rows=rows),
            "volume_delta": self.trade_analytics("volume-delta"),
            "cumulative_volume_delta": self.trade_analytics("cvd"),
            "open_interest": self.oi(),
            "volume_profile": self.trade_analytics("volume-profile", rows=rows),
            "volatility": self.wrap(
                {
                    f: {
                        "timestamp": r["exchange_ts"],
                        "realized": r["realized_volatility_20_unannualized"],
                    }
                    for f, r in (technical["data"] or {}).items()
                    if r
                },
                ts=technical["exchange_timestamp"],
                source=technical["source"],
            )
            if technical["data"]
            else self.wrap(),
            "anomalies": self.book("anomalies"),
            "historical_statistics": self.statistics(ResearchQuery(symbol=self.symbol), cache=False)
            if q.include_research
            else self.wrap(warnings=["Research omitted by request."]),
            "cross_market_context": self.cross_market(CrossQuery(symbol=q.symbol))
            if q.include_cross_market
            else self.wrap(warnings=["Cross-market analysis omitted by request."]),
        }
        degraded = any(
            v.get("data_quality_status") not in (None, "HEALTHY", "MARKET_CLOSED")
            for name, v in blocks.items()
            if name not in {"historical_statistics", "cross_market_context"}
        )
        warnings = ["See timestamps, warnings and freshness in each section."]
        return self.wrap(
            dict(blocks, instrument=self.symbol, warnings=warnings),
            ts=quote["exchange_timestamp"],
            source=quote["source"],
            quality="PARTIAL_DATA" if degraded else quote["data_quality_status"],
            warnings=warnings,
        )


def _trades(reader):
    return reader.page("trades")


def _large_trades(reader):
    return reader.page("trades", min_quantity=100)


ROUTES = {
    "/market/quote": (MarketQuery, MarketRead.quote),
    "/market/capabilities": (MarketQuery, MarketRead.capabilities),
    "/market/orderbook": (BookQuery, lambda r: r.book("book")),
    "/market/orderbook/history": (BookPageQuery, lambda r: r.page("book")),
    "/market/orderbook/imbalance": (BookQuery, lambda r: r.book("imbalance")),
    "/market/trades": (PageQuery, _trades),
    "/market/trades/large": (PageQuery, _large_trades),
    "/market/open-interest": (MarketQuery, MarketRead.oi),
    "/market/open-interest/history": (PageQuery, lambda r: r.page("oi")),
    "/market/volume-delta": (FlowQuery, lambda r: r.trade_analytics("volume-delta")),
    "/market/cvd": (FlowQuery, lambda r: r.trade_analytics("cvd")),
    "/market/volume-profile": (FlowQuery, lambda r: r.trade_analytics("volume-profile")),
    "/market/liquidity": (BookQuery, lambda r: r.book("liquidity")),
    "/market/indicators": (FrameQuery, MarketRead.technical),
    "/market/anomalies": (BookQuery, lambda r: r.book("anomalies")),
    "/market/correlations": (CrossQuery, MarketRead.cross_market),
    "/market/snapshot": (SnapshotQuery, MarketRead.snapshot),
    "/research/historical-analogs": (ResearchQuery, MarketRead.statistics),
    "/research/conditional-statistics": (ResearchQuery, MarketRead.statistics),
    "/health/data-quality": (MarketQuery, MarketRead.health),
}


def register(path, model, handle):
    def endpoint(request: Request, query):
        if not read_slots.acquire(blocking=False):
            raise HTTPException(429, "Market v2 is busy", headers={"Retry-After": "1"})
        try:
            store = MarketStore(os.getenv("MARKET_V2_READ_DATABASE_URL") or DATABASE_URL)
            with store.reader() as (conn, cutoff):
                result = handle(MarketRead(conn, cutoff, query, request.app))
                log.info(
                    "Market v2 read route=%s instrument=%s status=%s",
                    path,
                    result["instrument"],
                    result["data_quality_status"],
                )
                return result
        except psycopg.Error as exc:
            log.warning("Market v2 database error path=%s error=%s", path, type(exc).__name__)
            return response(
                query.symbol,
                datetime.now(UTC),
                query.max_age_seconds,
                "UNKNOWN",
                quality="API_UNAVAILABLE",
                warnings=["Market storage could not be read."],
            )

        finally:
            read_slots.release()

    endpoint.__name__ = path.strip("/").replace("/", "_").replace("-", "_")
    endpoint.__annotations__["query"] = Annotated[model, Query()]
    router.add_api_route(path, endpoint, methods=["GET"], dependencies=[Depends(rate_limit)])


for route, (query_model, handler) in ROUTES.items():
    register(route, query_model, handler)
