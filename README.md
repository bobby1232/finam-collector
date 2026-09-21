# Finam Quotes Collector

Railway-ready collector for Finam Trade API bars -> PostgreSQL.

## Configuration

Required:

```env
FINAM_SECRET=...
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

Instruments are configured in one variable:

```env
INSTRUMENTS=GAZP@MISX:TIME_FRAME_M1,SiZ6@RTSX:TIME_FRAME_M1
POLL_SECONDS=30
```

`SiZ6` is the MOEX Si-12.26 USD/RUB futures contract, expiration 2026-12-17.

Supported timeframes:
`TIME_FRAME_M1`, `M5`, `M15`, `M30`, `H1`, `H2`, `H4`, `H8`, `D`, `W`, `MN`, `QR` (with the `TIME_FRAME_` prefix).

## Storage

Table `candles`:

- `ticker`
- `timeframe`
- `ts`
- `open`
- `high`
- `low`
- `close`
- `volume` — volume returned by Finam for the bar
- `updated_at`

Primary key: `(ticker, timeframe, ts)`. Recent/incomplete candles are refreshed using UPSERT.

## Endpoints

- `GET /health`
- `GET /stats` — includes volume completeness counters
- `GET /bars?symbol=SiZ6@RTSX&timeframe=TIME_FRAME_M1&limit=20`

## Local run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```
