# Market Data v2

## Аудит

База аудита — `956e982`. FastAPI запускает Finam REST-сбор свечей и MOEX ISS-дозагрузку. PostgreSQL хранит `candles` и `backfill_status`. Существующий MCP использует OAuth Keycloak, Caddy и отдельный токен для защищённых REST-чтений. Текущие настройки интервала в Git и production различаются. Проверенный MCP-вызов вернул `ok`, `mcp_ready=true`, 0 ошибок и 30 с. Последняя свеча была от 20:49 UTC, а успешная загрузка была на 17 секунд старше вызова около 22:48 UTC. Причины ранее наблюдавшихся трёх ошибок требуют серверных логов за тот период. `SI@CONT` по историческим дням выбирает контракт по итоговому объёму дня, а текущий Finam-символ в исходной ветке задан константой. Для исследований по свечным горизонтам v2 использует конкретный контракт.

| Данные | Источник и метод | Частота и история | Ограничения |
| --- | --- | --- | --- |
| OHLCV | Finam REST `Bars`, MOEX ISS для дозагрузки | Настраиваемый polling. Finam M1 за 7 дней, M5 и ряд других интервалов за 30 дней | Незавершённые свечи требуют маркировки. Исторические данные MOEX могут иметь другую методику |
| Котировка | Finam REST `LastQuote`, gRPC `SubscribeQuote`, WebSocket `QUOTES` | Последняя котировка или поток | Поля зависят от инструмента и прав. Котировка свечного fallback имеет другую семантику |
| Level 2 | Finam REST `OrderBook`, `SubscribeOrderBook` или WebSocket `ORDER_BOOK` | Снимок или изменения. История с начала собственного сбора | Нет документированного sequence ID, числа заявок и гарантированной глубины. Потерю дельт без разрыва нельзя доказать |
| Публичные сделки | Finam `LatestTrades`, `SubscribeLatestTrades` или `INSTRUMENT_TRADES` | Последняя пачка или поток. История с начала записи | Нет документированного replay с курсором. Ограниченная пачка после разрыва может не покрыть разрыв. Сторона описана как сторона сделки |
| OI | Finam protobuf `Quote.open_interest`, `Trade.open_interest` | В котировке или на момент сделки, если поле присутствует. Собственная история | Периодичность и доступ конкретного счёта требуют проверки. MOEX также публикует OI с задержкой и предлагает отдельный продукт FUTOI |
| Межрыночные ряды | Finam Bars и MOEX ISS по конкретному инструменту | По наличию сохранённых OHLCV | Нужна проверка мэппинга и лицензии для Si, USD/RUB, CNY/RUB, CNY futures, IMOEX, RTSI, Brent и Gold. Нельзя переносить цену через закрытую сессию |

Источники — [Finam REST](https://api.finam.ru/docs/rest/), [Finam gRPC](https://api.finam.ru/docs/grpc/), [Finam WebSocket](https://api.finam.ru/docs/async-api/), [MarketData protobuf](https://github.com/FinamWeb/finam-trade-api/blob/45c71d40ad0c9a147bbbc63ac8cda462f74eb87e/proto/grpc/tradeapi/v1/marketdata/marketdata_service.proto), [лимиты Finam](https://api.finam.ru/getting-started/chto-delat-kogda-api-otvechaet-ne-tak/limity-zaprosov/), [MOEX ISS](https://www.moex.com/files/4be999zbzp80bx2bgmwayrtyx0), [MOEX FUTOI](https://www.moex.com/en/analyticalproducts). Finam публикует лимит 200 запросов в минуту на метод. Права аккаунта, лимиты одновременных подписок, разрешение на хранение и передачу данных нужно сверить перед включением сбора.

## Спринты

1. P0. Хранилище Level 2, сделок, OI и котировок. Защищённый API и расширение существующего MCP. PostgreSQL и интеграционный прогон.
2. P1. Аналитика потока, профиль, свечные индикаторы, исследования по конкретным контрактам, межрыночное сопоставление, REST-клиент и Coolify runbook.
3. Валидация на staging. Проверка прав Finam, формата потока, спецификации биржевого календаря, реальной частоты OI, нагрузки и discovery в ChatGPT. Затем отдельно согласовать production.
