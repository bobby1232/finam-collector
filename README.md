# Finam Quotes Collector

Small Railway service that polls Finam Trade API candles and stores them in PostgreSQL.

## Required variables

- `FINAM_SECRET` — Finam Trade API secret token.
- `DATABASE_URL` — PostgreSQL connection string.
- `TICKER` — instrument in `ticker@mic` format, e.g. `GAZP@MISX`.
- `TIMEFRAME` — e.g. `TIME_FRAME_M1`, `TIME_FRAME_M5`, `TIME_FRAME_H1`, `TIME_FRAME_D`.

Optional:
- `POLL_SECONDS` — polling frequency. If omitted, chosen automatically from timeframe.
- `LOOKBACK_DAYS` — initial history depth. Defaults to the maximum documented Finam history depth for the selected timeframe.

## Database

Table `candles` is created automatically. Primary key is `(ticker, timeframe, ts)`.
The current candle can be requested repeatedly: `ON CONFLICT ... DO UPDATE` updates it rather than creating duplicates.

## Endpoints

- `GET /health`
- `GET /stats`

## Local run

```bash
pip install -r requirements.txt
export FINAM_SECRET='...'
export DATABASE_URL='postgresql://...'
export TICKER='GAZP@MISX'
export TIMEFRAME='TIME_FRAME_M1'
uvicorn app.main:app --reload
```
