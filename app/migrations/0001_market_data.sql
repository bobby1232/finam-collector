-- Additive migration. The application takes an advisory transaction lock before applying it.
CREATE TABLE IF NOT EXISTS market_instruments (
    symbol text PRIMARY KEY,
    source text NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol text NOT NULL REFERENCES market_instruments,
    source text NOT NULL,
    exchange_ts timestamptz,
    received_ts timestamptz NOT NULL,
    fingerprint text NOT NULL,
    sequence_id text,
    quality text NOT NULL,
    UNIQUE (symbol, source, fingerprint)
);
CREATE INDEX IF NOT EXISTS market_book_history
    ON orderbook_snapshots (symbol, received_ts DESC, id DESC);
CREATE TABLE IF NOT EXISTS orderbook_levels (
    snapshot_id bigint NOT NULL REFERENCES orderbook_snapshots ON DELETE CASCADE,
    side text NOT NULL CHECK (side IN ('bid', 'ask')),
    price numeric NOT NULL,
    quantity numeric NOT NULL CHECK (quantity > 0),
    orders_count integer,
    PRIMARY KEY (snapshot_id, side, price)
);
CREATE TABLE IF NOT EXISTS market_trades (
    symbol text NOT NULL REFERENCES market_instruments,
    source text NOT NULL,
    trade_id text NOT NULL,
    exchange_ts timestamptz NOT NULL,
    received_ts timestamptz NOT NULL,
    price numeric NOT NULL,
    quantity numeric NOT NULL CHECK (quantity > 0),
    side text NOT NULL CHECK (side IN ('BUY', 'SELL', 'UNKNOWN')),
    side_method text NOT NULL,
    PRIMARY KEY (symbol, source, trade_id)
);
CREATE INDEX IF NOT EXISTS market_trade_history
    ON market_trades (symbol, exchange_ts DESC, trade_id DESC);
CREATE TABLE IF NOT EXISTS market_quotes (
    symbol text NOT NULL REFERENCES market_instruments,
    source text NOT NULL,
    exchange_ts timestamptz NOT NULL,
    received_ts timestamptz NOT NULL,
    quote jsonb NOT NULL,
    PRIMARY KEY (symbol, source, exchange_ts)
);
CREATE INDEX IF NOT EXISTS market_quote_history ON market_quotes (symbol, exchange_ts DESC);
CREATE TABLE IF NOT EXISTS open_interest_history (
    symbol text NOT NULL REFERENCES market_instruments,
    source text NOT NULL,
    exchange_ts timestamptz NOT NULL,
    received_ts timestamptz NOT NULL,
    open_interest numeric NOT NULL CHECK (open_interest >= 0),
    PRIMARY KEY (symbol, source, exchange_ts)
);
CREATE INDEX IF NOT EXISTS market_oi_history ON open_interest_history (symbol, exchange_ts DESC);
CREATE TABLE IF NOT EXISTS ingestion_health (
    symbol text NOT NULL REFERENCES market_instruments,
    feed text NOT NULL,
    status text NOT NULL,
    updated_at timestamptz NOT NULL,
    detail jsonb NOT NULL,
    PRIMARY KEY (symbol, feed)
);
CREATE TABLE IF NOT EXISTS market_flow_aggregates (
    symbol text NOT NULL REFERENCES market_instruments,
    interval_seconds int NOT NULL CHECK (interval_seconds IN (15, 60)),
    bucket_ts timestamptz NOT NULL,
    latest_trade_ts timestamptz NOT NULL,
    buy_volume numeric NOT NULL,
    sell_volume numeric NOT NULL,
    unknown_volume numeric NOT NULL,
    trades_count bigint NOT NULL,
    PRIMARY KEY (symbol, interval_seconds, bucket_ts)
);
