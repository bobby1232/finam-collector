from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

Frame = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


def integer_query(value):
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else value


Depth = Annotated[Literal[5, 10, 20, 50], BeforeValidator(integer_query)]
FlowInterval = Annotated[Literal[15, 60, 300, 900, 1800, 3600], BeforeValidator(integer_query)]
Symbol = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,32}@[A-Za-z0-9]{2,12}$")]
Cursor = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,256}$")]
Age = Annotated[int, Field(ge=15, le=86400)]

FRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


class MarketQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Symbol = "SI@CONT"
    max_age_seconds: Age = 180


class BookQuery(MarketQuery):
    orderbook_depth: Depth = 20


class PageQuery(MarketQuery):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Cursor | None = None
    history_window: Annotated[int, Field(ge=60, le=31 * 86400)] = 3600


class BookPageQuery(BookQuery, PageQuery):
    pass


class FlowQuery(MarketQuery):
    interval: FlowInterval = 60
    history_window: Annotated[int, Field(ge=60, le=86400)] = 3600


class FrameQuery(MarketQuery):
    timeframes: Annotated[list[Frame], Field(min_length=1, max_length=7)] = ["1m", "15m"]


class ResearchQuery(MarketQuery):
    timeframe: Frame = "15m"
    history_window: Annotated[int, Field(ge=86400, le=365 * 86400)] = 30 * 86400
    conditions: Annotated[
        list[
            Literal[
                "rsi_lt_30",
                "rsi_lt_25",
                "rsi_lt_20",
                "rsi_gt_70",
                "rsi_gt_75",
                "rsi_gt_80",
                "price_drop",
                "price_rise",
                "volume_anomaly",
                "vwap_deviation",
                "oi_anomaly",
                "price_cvd_divergence",
                "price_oi_divergence",
                "book_imbalance",
            ]
        ],
        Field(min_length=1, max_length=6),
    ] = ["rsi_lt_30"]


class CrossQuery(MarketQuery):
    comparison_symbol: Symbol | None = None
    timeframe: Frame = "15m"
    history_window: Annotated[int, Field(ge=3600, le=30 * 86400)] = 86400 * 7


class SnapshotQuery(BookQuery, FrameQuery):
    history_window: Annotated[int, Field(ge=60, le=86400)] = 3600
    include_research: bool = False
    include_cross_market: bool = False
