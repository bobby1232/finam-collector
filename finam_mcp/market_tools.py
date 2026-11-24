from datetime import UTC, datetime
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from marketdata.contracts import (
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

from .collector import CollectorClient, CollectorError
from .rate_limit import SlidingWindowRateLimiter

TOOLS: dict[str, tuple[str, type[MarketQuery], str]] = {
    "get_market_capabilities": (
        "market/capabilities",
        MarketQuery,
        "Observed data and provider limitations.",
    ),
    "get_market_snapshot": (
        "market/snapshot",
        SnapshotQuery,
        "Concurrent market inputs at a shared storage cutoff.",
    ),
    "get_orderbook": ("market/orderbook", BookQuery, "Latest observed Level 2 price levels."),
    "get_orderbook_history": (
        "market/orderbook/history",
        BookPageQuery,
        "Recorded Level 2 snapshots, newest first.",
    ),
    "get_orderbook_imbalance": (
        "market/orderbook/imbalance",
        BookQuery,
        "Visible (bid - ask) / (bid + ask) depth.",
    ),
    "get_recent_trades": (
        "market/trades",
        PageQuery,
        "Public exchange trades with reported side and timestamps.",
    ),
    "get_open_interest": (
        "market/open-interest",
        MarketQuery,
        "Latest reported OI and same-source changes.",
    ),
    "get_open_interest_history": (
        "market/open-interest/history",
        PageQuery,
        "Recorded provider OI, newest first.",
    ),
    "get_volume_delta": (
        "market/volume-delta",
        FlowQuery,
        "Observed buy minus sell volume in UTC buckets.",
    ),
    "get_cvd": ("market/cvd", FlowQuery, "Observed cumulative volume delta from window start."),
    "get_volume_profile": (
        "market/volume-profile",
        FlowQuery,
        "Trade-price volume, POC and 70% value area.",
    ),
    "get_liquidity_levels": (
        "market/liquidity",
        BookQuery,
        "Largest visible levels and changes since the prior snapshot.",
    ),
    "get_large_trades": (
        "market/trades/large",
        PageQuery,
        "Recorded exchange trades of 100 contracts or more.",
    ),
    "get_market_anomalies": ("market/anomalies", BookQuery, "Visible liquidity change heuristic."),
    "get_technical_indicators": (
        "market/indicators",
        FrameQuery,
        "Indicators calculated on completed UTC candle buckets.",
    ),
    "get_cross_market_analysis": (
        "market/correlations",
        CrossQuery,
        "Correlation and lead/lag for aligned observed returns.",
    ),
    "get_historical_analogs": (
        "research/historical-analogs",
        ResearchQuery,
        "Historical crossings of combined conditions.",
    ),
    "get_conditional_statistics": (
        "research/conditional-statistics",
        ResearchQuery,
        "Descriptive outcomes of historical filters.",
    ),
    "get_market_data_health": (
        "health/data-quality",
        MarketQuery,
        "Feed health, data freshness and storage footprint.",
    ),
}


def register_market_tools(
    server: MCPServer,
    client: CollectorClient,
    limiter: SlidingWindowRateLimiter,
    annotations: ToolAnnotations,
) -> None:
    def register(name: str, path: str, model: type[MarketQuery], description: str) -> None:
        async def call(query: MarketQuery | None = None) -> dict[str, Any]:
            await limiter.check()
            request = query or model()
            try:
                return await client.market_v2(path, request.model_dump(exclude_none=True))
            except CollectorError:
                return {
                    "instrument": request.symbol,
                    "source": None,
                    "exchange_timestamp": None,
                    "server_timestamp": datetime.now(UTC).isoformat(),
                    "data_age_seconds": None,
                    "is_fresh": False,
                    "data_quality_status": "API_UNAVAILABLE",
                    "market_session_status": "UNKNOWN",
                    "warnings": ["Market v2 request failed or exceeded the response budget."],
                    "schema_version": "2.0",
                    "data": None,
                }

        call.__annotations__["query"] = model | None
        server.add_tool(
            call,
            name=name,
            description=(
                description + " Inspect data_quality_status, timestamps, is_fresh and warnings. "
                "A null value means no observation or insufficient data. Maximum 1 MiB."
                + (
                    " Forward next_cursor as query.cursor to page."
                    if issubclass(model, PageQuery)
                    else ""
                )
            ),
            annotations=annotations,
            structured_output=True,
        )

    for name, (path, model, description) in TOOLS.items():
        register(name, path, model, description)
