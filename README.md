# Finam Quotes Collector

Railway-ready collector for Finam Trade API bars -> PostgreSQL.

The repository also contains a separate, OAuth-protected, read-only MCP service for ChatGPT.
Deployment and security instructions: [docs/CHATGPT_MCP_SETUP.md](docs/CHATGPT_MCP_SETUP.md).

## Configuration

Required:

```env
FINAM_SECRET=...
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

Instruments are configured in one variable:

```env
INSTRUMENTS=GAZP@MISX:TIME_FRAME_M1,SiZ6@RTSX:TIME_FRAME_M1
POLL_SECONDS=1
FINAM_MAX_REQUESTS_PER_SECOND=3
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

## Market Data v2

Ветка v2 добавляет защищённый REST API и 19 read-only инструментов MCP для рыночных
данных и исследований. Сбор v2 включается через `MARKET_V2_MODE`. По умолчанию
режим `off`, существующие API и MCP инструменты доступны.

- [Аудит источников и roadmap](ROADMAP_MARKET_DATA_V2.md)
- [Контракты API и MCP, проверка и результаты](docs/MARKET_DATA_V2.md)
- [Миграция, Coolify и откат](docs/MARKET_DATA_V2_RUNBOOK.md)
