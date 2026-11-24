from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .analytics import completed_bars, is_complete, technical_analysis, wilder_rsi
from .auth import KeycloakJWTVerifier
from .collector import CollectorClient, CollectorError
from .config import Settings, get_settings
from .market_tools import register_market_tools
from .rate_limit import SlidingWindowRateLimiter

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _age_seconds(timestamp: str | None, now: datetime | None = None) -> float | None:
    if not timestamp:
        return None
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, ((now or _now()) - parsed).total_seconds())


def create_server(
    settings: Settings | None = None,
    collector: CollectorClient | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    settings = settings or get_settings()
    client = collector or CollectorClient(settings)
    limiter = SlidingWindowRateLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        yield {"collector": client}
        await client.close()

    server = MCPServer(
        name="finam-mcp",
        title="Finam Collector (read-only)",
        version="1.0.0",
        description="Read-only Si futures market data and descriptive technical analytics.",
        instructions=(
            "Use these tools to read stored market data and descriptive analytics. "
            "The latest price is the close-so-far of a one-minute candle, not a tick quote. "
            "Never describe results as guaranteed, executable, or investment advice."
        ),
        token_verifier=token_verifier or KeycloakJWTVerifier(settings),
        auth=AuthSettings(
            issuer_url=settings.oauth_issuer_url,
            resource_server_url=settings.mcp_resource_url,
            required_scopes=[settings.mcp_required_scope],
            validate_token_resource=True,
            service_documentation_url=settings.mcp_resource_url,
        ),
        lifespan=lifespan,
        log_level=settings.log_level,
    )

    @server.custom_route("/healthz", ["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> JSONResponse:
        started = time.perf_counter()
        try:
            health = await client.health()
            bars = await client.bars("SiZ6@RTSX", "1m", 1)
            latest = bars[-1] if bars else None
            age = _age_seconds(latest.get("updated_at") if latest else None)
            ready = health.get("status") == "ok" and latest is not None and age is not None
            return JSONResponse(
                {
                    "status": "ok" if ready else "degraded",
                    "oauth_required": True,
                    "collector_status": health.get("status"),
                    "poll_seconds": health.get("poll_seconds"),
                    "cycle_running": health.get("cycle_running"),
                    "cycle_duration_seconds": health.get("cycle_duration_seconds"),
                    "finam_api": health.get("finam_api", {}),
                    "last_bar_timestamp": latest.get("timestamp") if latest else None,
                    "last_ingest_age_seconds": round(age, 3) if age is not None else None,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                },
                status_code=200 if ready else 503,
            )
        except CollectorError:
            return JSONResponse({"status": "unavailable", "oauth_required": True}, status_code=503)

    @server.tool(
        description=(
            "Return the latest Si one-minute candle close-so-far from Finam Collector. "
            "This is not a tick/realtime executable quote; freshness and completion flags "
            "are explicit."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_si_quote(
        symbol: Annotated[
            Literal["SiZ6@RTSX", "SI@CONT"],
            Field(description="Si futures symbol; SiZ6@RTSX is the current configured contract."),
        ] = "SiZ6@RTSX",
        max_age_seconds: Annotated[
            int,
            Field(
                ge=30,
                le=3600,
                description="Maximum acceptable age of the current one-minute candle/ingest.",
            ),
        ] = 180,
    ) -> dict[str, Any]:
        await limiter.check()
        bars = await client.bars(symbol, "1m", 1)
        if not bars:
            raise CollectorError("No Si candles are available")
        bar = bars[-1]
        now = _now()
        bar_age = _age_seconds(bar["timestamp"], now)
        ingest_age = _age_seconds(bar.get("updated_at"), now)
        fresh = (
            bar_age is not None
            and ingest_age is not None
            and bar_age <= max_age_seconds + 60
            and ingest_age <= max_age_seconds
        )
        timestamp = datetime.fromisoformat(bar["timestamp"])
        return {
            "instrument": symbol,
            "symbol": symbol,
            "price": bar["close"],
            "price_semantics": "close-so-far of latest 1-minute candle; not a tick quote",
            "timestamp_utc": timestamp.astimezone(UTC).isoformat(),
            "timestamp_msk": timestamp.astimezone(ZoneInfo("Europe/Moscow")).isoformat(),
            "server_time_utc": now.isoformat(),
            "age_seconds": round(bar_age, 3) if bar_age is not None else None,
            "age_semantics": (
                "Age is measured from the one-minute candle OPEN timestamp; it is not exact "
                "exchange-feed latency. ingest_age_seconds measures local collector update age."
            ),
            "source": bar.get("source") or "finam-collector",
            "timeframe": "1m",
            "candle_complete": is_complete(bar, "1m", now),
            "ingest_age_seconds": round(ingest_age, 3) if ingest_age is not None else None,
            "max_age_seconds": max_age_seconds,
            "is_fresh": fresh,
            "market_session_status": "unknown",
            "data_quality_status": "fresh" if fresh else "stale",
            "warning": None if fresh else "Latest Si candle is older than the requested threshold.",
        }

    @server.tool(
        description=(
            "Calculate Wilder RSI from completed Si candles. The current incomplete candle "
            "is excluded so the value does not masquerade as a settled indicator."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_si_rsi(
        symbol: Literal["SiZ6@RTSX", "SI@CONT"] = "SiZ6@RTSX",
        timeframes: Annotated[
            tuple[Literal["1m", "5m", "15m", "1h"], ...],
            Field(min_length=1, max_length=4),
        ] = ("15m",),
        period: Annotated[int, Field(ge=2, le=100)] = 14,
    ) -> dict[str, Any]:
        await limiter.check()
        unique_timeframes = list(dict.fromkeys(timeframes))
        results: dict[str, Any] = {}
        for timeframe in unique_timeframes:
            raw = await client.bars(symbol, timeframe, min(500, max(120, period * 5)))
            bars, excluded = completed_bars(raw, timeframe)
            closes = [float(x["close"]) for x in bars]
            value = wilder_rsi(closes, period)
            if value is None:
                raise CollectorError(f"Not enough completed {timeframe} candles for RSI")
            candle_age = _age_seconds(bars[-1]["timestamp"])
            freshness_limit = max(
                settings.freshness_seconds,
                3
                * {
                    "1m": 60,
                    "5m": 300,
                    "15m": 900,
                    "1h": 3600,
                }[timeframe],
            )
            results[timeframe] = {
                "rsi": round(value, 4),
                "calculated_at": _now().isoformat(),
                "last_candle_timestamp": bars[-1]["timestamp"],
                "last_candle_complete": True,
                "candles_used": len(bars),
                "incomplete_current_candle_excluded": excluded,
                "data_age_seconds": round(candle_age, 3) if candle_age is not None else None,
                "is_fresh": candle_age is not None and candle_age <= freshness_limit,
            }
        return {
            "symbol": symbol,
            "period": period,
            "results": results,
            "methodology": "Wilder RSI calculated from completed candle closes.",
        }

    @server.tool(
        description=(
            "Return historical Si OHLCV candles in chronological order. Maximum 500 rows; "
            "read-only."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_si_candles(
        symbol: Literal["SiZ6@RTSX", "SI@CONT"] = "SiZ6@RTSX",
        timeframe: Literal["1m", "5m", "15m", "1h"] = "15m",
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        completed_only: bool = True,
    ) -> dict[str, Any]:
        await limiter.check()
        raw = await client.bars(symbol, timeframe, min(500, limit + 1))
        if completed_only:
            bars, excluded = completed_bars(raw, timeframe)
        else:
            bars, excluded = raw, False
        bars = bars[-limit:]
        latest_age = _age_seconds(bars[-1]["timestamp"] if bars else None)
        freshness_limit = max(
            settings.freshness_seconds,
            3
            * {
                "1m": 60,
                "5m": 300,
                "15m": 900,
                "1h": 3600,
            }[timeframe],
        )
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "count": len(bars),
            "completed_only": completed_only,
            "incomplete_current_candle_excluded": excluded,
            "server_time_utc": _now().isoformat(),
            "latest_timestamp": bars[-1]["timestamp"] if bars else None,
            "latest_age_seconds": round(latest_age, 3) if latest_age is not None else None,
            "age_semantics": "Age is measured from the candle open timestamp.",
            "is_fresh": latest_age is not None and latest_age <= freshness_limit,
            "bars": bars,
        }

    @server.tool(
        description=(
            "Build a descriptive Si technical snapshot using completed candles: RSI14, EMA20/50, "
            "ATR14, simple trend and 20-candle levels. Not investment advice."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_si_analysis(
        symbol: Literal["SiZ6@RTSX", "SI@CONT"] = "SiZ6@RTSX",
        timeframe: Literal["1m", "5m", "15m", "1h"] = "15m",
    ) -> dict[str, Any]:
        await limiter.check()
        raw = await client.bars(symbol, timeframe, 220)
        bars, excluded = completed_bars(raw, timeframe)
        if len(bars) < 51:
            raise CollectorError("At least 51 completed candles are required for analysis")
        analysis = technical_analysis(bars, timeframe)
        quote_bars = await client.bars(symbol, "1m", 1)
        quote = quote_bars[-1] if quote_bars else None
        quote_age = _age_seconds(quote.get("timestamp") if quote else None)
        ingest_age = _age_seconds(quote.get("updated_at") if quote else None)
        fresh = bool(
            quote
            and quote_age is not None
            and ingest_age is not None
            and quote_age <= settings.freshness_seconds + 60
            and ingest_age <= settings.freshness_seconds
        )
        recent_low = analysis["support_20"]
        recent_high = analysis["resistance_20"]
        price = quote["close"] if quote else analysis["close"]
        span = recent_high - recent_low
        analysis.update(
            {
                "symbol": symbol,
                "current_available_price": price,
                "current_price_semantics": "latest 1-minute candle close-so-far",
                "current_price_timestamp": quote.get("timestamp") if quote else None,
                "data_age_seconds": round(quote_age, 3) if quote_age is not None else None,
                "ingest_age_seconds": round(ingest_age, 3) if ingest_age is not None else None,
                "is_fresh": fresh,
                "data_quality_status": "fresh" if fresh else "stale",
                "position_in_20_candle_range_pct": (
                    round(100 * (float(price) - recent_low) / span, 2) if span > 0 else None
                ),
                "incomplete_current_candle_excluded": excluded,
                "generated_at": _now().isoformat(),
            }
        )
        return analysis

    @server.tool(
        description=(
            "Return the collector's protected historical RSI oversold research. Results are "
            "descriptive statistics, not a trading simulation or forecast."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_si_oversold_research(
        symbol: Literal["SiZ6@RTSX", "SI@CONT"] = "SI@CONT",
        timeframe: Literal["15m", "1h", "1d"] = "1h",
        thresholds: Annotated[
            str, Field(pattern=r"^\d+(?:\.\d+)?(?:,\d+(?:\.\d+)?)*$")
        ] = "30,25,20",
        horizons: Annotated[str, Field(pattern=r"^\d+(?:,\d+)*$")] = "1,3,5,10",
    ) -> dict[str, Any]:
        await limiter.check()
        result = await client.oversold_research(symbol, timeframe, thresholds, horizons)
        result["generated_at"] = _now().isoformat()
        result["warning"] = (
            "Historical probabilities are descriptive and do not guarantee future results."
        )
        return result

    @server.tool(
        description=(
            "Check Finam Collector availability, latest Si ingestion age, and MCP readiness. "
            "No credentials or internal network details are returned."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_collector_health() -> dict[str, Any]:
        await limiter.check()
        started = time.perf_counter()
        health = await client.health()
        bars = await client.bars("SiZ6@RTSX", "1m", 1)
        latest = bars[-1] if bars else None
        ingest_age = _age_seconds(latest.get("updated_at") if latest else None)
        return {
            "status": health.get("status"),
            "collector_api_reachable": True,
            "mcp_ready": health.get("status") == "ok" and latest is not None,
            "poll_seconds": health.get("poll_seconds"),
            "last_cycle": health.get("last_cycle"),
            "latest_si_candle": latest.get("timestamp") if latest else None,
            "latest_ingest_age_seconds": (round(ingest_age, 3) if ingest_age is not None else None),
            "request_latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "instrument_errors": sum(
                1 for item in health.get("instruments", {}).values() if item.get("last_error")
            ),
        }

    register_market_tools(server, client, limiter, READ_ONLY)
    return server


def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    server = create_server(settings)
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=1_048_576,
        max_sessions=100,
        session_idle_timeout=300,
    )


if __name__ == "__main__":
    main()
