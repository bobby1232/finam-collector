# Finam Collector MCP для ChatGPT

## Архитектура

```text
ChatGPT
  -> HTTPS https://home-server.taile968d8.ts.net/mcp
  -> Tailscale Funnel (только публичный HTTPS :443)
  -> 127.0.0.1:8765 / Caddy allowlist gateway
       -> /mcp и Protected Resource Metadata -> finam-mcp:8000
       -> /oauth/realms/finam и /oauth/resources -> Keycloak:8080
       -> всё остальное -> 404
  -> Finam Collector http://100.103.115.28:8081 (только чтение)
```

`finam-mcp` не подключается к PostgreSQL и не знает токен Finam. Он читает только уже
существующий HTTP API коллектора. Внешний пользователь получает короткоживущий JWT Keycloak;
MCP проверяет подпись, точный `issuer`, `audience`, `exp` и scope `finam:read`.

Keycloak Admin Console, master realm, Coolify, PostgreSQL и исходный FastAPI через Funnel не
маршрутизируются.

## Переменные окружения

Шаблон находится в `deploy/finam-mcp/.env.example`. В Coolify задать:

| Переменная | Значение/назначение |
|---|---|
| `MCP_PUBLIC_HOST` | `home-server.taile968d8.ts.net` |
| `MCP_RESOURCE_URL` | `https://home-server.taile968d8.ts.net/mcp` |
| `OAUTH_ISSUER_URL` | `https://home-server.taile968d8.ts.net/oauth/realms/finam` |
| `COLLECTOR_BASE_URL` | `http://100.103.115.28:8081` |
| `RESEARCH_API_TOKEN` | действующий токен `/api/research/si/oversold` |
| `KEYCLOAK_ADMIN_USERNAME` | локальный администратор Keycloak |
| `KEYCLOAK_ADMIN_PASSWORD` | случайный пароль минимум 32 символа |
| `KEYCLOAK_DB_PASSWORD` | случайный пароль отдельной БД Keycloak |
| `CHATGPT_OAUTH_CLIENT_SECRET` | случайный OAuth client secret |
| `FINAM_MCP_USERNAME` | пользователь для входа ChatGPT, например `ilya` |
| `FINAM_MCP_USER_PASSWORD` | отдельный случайный пароль OAuth-пользователя |

Секреты можно создать командой `openssl rand -base64 48`. Не помещать `.env` в Git, логи или
скриншоты. После возможной утечки менять соответствующий секрет и завершать активные сессии.

## Развёртывание в Coolify

1. Создать новый ресурс Docker Compose из репозитория `bobby1232/finam-collector`, ветка `main`.
2. Compose path: `/deploy/finam-mcp/docker-compose.yml`.
3. Внести перечисленные переменные в Secrets/Environment Variables Coolify.
4. Не добавлять домен через proxy Coolify и не публиковать другие порты. Gateway привязан только
   к `127.0.0.1:8765` хоста.
5. Выполнить Deploy и дождаться healthy для `keycloak-db`, `keycloak`, `finam-mcp`, `gateway`.
6. Проверить автоматический restart policy: у всех сервисов `unless-stopped`.

Realm импортируется только в пустую БД Keycloak. При последующих обновлениях существующий realm
не перезаписывается — это защищает пользователей и настройки. Изменения realm после первого
запуска следует применять через локальную Admin Console/CLI, а не удалением volume.

### Локальные проверки до Funnel

На домашнем сервере:

```bash
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/.well-known/oauth-protected-resource/mcp
curl -fsS http://127.0.0.1:8765/oauth/realms/finam/.well-known/openid-configuration
curl -i http://127.0.0.1:8765/mcp
curl -i http://127.0.0.1:8765/oauth/admin/
```

Ожидания: первые три запроса успешны, `/mcp` без JWT возвращает `401`, admin возвращает `404`.
В metadata должны быть точные HTTPS URL без `localhost` и без `:8765`.

## OAuth

Используется Keycloak 26.8.2, Authorization Code Flow, PKCE S256 и Resource Indicators RFC 8707.
Access token живёт 300 секунд. Статический клиент:

- Client ID: `chatgpt-finam-mcp`
- Client secret: значение `CHATGPT_OAUTH_CLIENT_SECRET`
- Redirect URI: `https://chatgpt.com/connector_platform_oauth_redirect`
- Scope: `finam:read`
- Resource/audience: `https://home-server.taile968d8.ts.net/mcp`

Authorization Server Metadata доступна по стандартному URL внутри realm. MCP SDK публикует
Protected Resource Metadata по
`/.well-known/oauth-protected-resource/mcp`. JWT отклоняется при неверных подписи, issuer,
audience, scope, `exp` или `nbf`.

Admin UI намеренно недоступна публично. При необходимости администрирования создайте временный
SSH port-forward к внутреннему порту или используйте терминал контейнера в Coolify; не меняйте
allowlist gateway.

## Tailscale Funnel

Включать только после локальных проверок выше.

```bash
sudo tailscale funnel status
sudo tailscale funnel --bg 8765
sudo tailscale funnel status
```

Если CLI показывает URL подтверждения, открыть его под владельцем tailnet и разрешить Funnel для
этого узла. Не включать Funnel для 8000, 8001, 8081, 5432, 22, 80 или панели Coolify.

После включения:

```bash
curl -fsS https://home-server.taile968d8.ts.net/healthz
curl -fsS https://home-server.taile968d8.ts.net/.well-known/oauth-protected-resource/mcp
curl -fsS https://home-server.taile968d8.ts.net/oauth/realms/finam/.well-known/openid-configuration
curl -i https://home-server.taile968d8.ts.net/mcp
curl -i https://home-server.taile968d8.ts.net/oauth/admin/
```

Нужны HTTPS `200/200/200/401/404`. Затем проверить авторизационный код с PKCE, `initialize`,
`tools/list` и `tools/call`. До успешной авторизованной проверки endpoint нельзя считать готовым.

## Подключение за 5 минут

Этот раздел применим только после статуса healthy и HTTPS-проверок.

1. В ChatGPT открыть Settings → Connectors (или Apps) → Add custom MCP.
2. URL: `https://home-server.taile968d8.ts.net/mcp`.
3. Authentication: OAuth.
4. Client ID: `chatgpt-finam-mcp`.
5. Client secret: секрет из Coolify.
6. Scope: `finam:read`.
7. Пройти вход пользователем `FINAM_MCP_USERNAME` и его отдельным паролем.
8. Проверить запрос: «Проверь свежесть котировок Si и RSI на 1, 5, 15 и 60 минутах».

Ожидаемые MCP tools:

- `get_si_quote`
- `get_si_rsi`
- `get_si_candles`
- `get_si_analysis`
- `get_si_oversold_research`
- `get_collector_health`

## Тестирование

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-mcp-dev.txt
.venv/bin/ruff format --check finam_mcp tests
.venv/bin/ruff check finam_mcp tests
.venv/bin/mypy finam_mcp
.venv/bin/pytest -q
.venv/bin/pip-audit -r requirements-mcp.txt
docker build -f Dockerfile.mcp -t finam-mcp:local .
docker compose -f deploy/finam-mcp/docker-compose.yml --env-file /secure/path/mcp.env config
```

CI также собирает образ и сканирует его Trivy на HIGH/CRITICAL уязвимости.

## Диагностика

| Симптом | Проверка |
|---|---|
| `401` без токена | Нормально: production MCP не анонимен |
| OAuth discovery содержит HTTP/localhost | Проверить `MCP_PUBLIC_HOST`, `MCP_RESOURCE_URL`, `OAUTH_ISSUER_URL` |
| `invalid_target` | Resource должен точно совпадать с `MCP_RESOURCE_URL` |
| `invalid_token` | Проверить время сервера, issuer, audience, scope и JWKS |
| ChatGPT возвращается с redirect mismatch | Использовать ровно зарегистрированный redirect URI ChatGPT |
| `/healthz` = 503 | Проверить доступ контейнера к `100.103.115.28:8081` и `/health` коллектора |
| Данные stale | Не перезапускать/не подменять цену; проверить collector last cycle и Finam source |
| Realm не обновился | Startup import пропускает существующий realm; применить изменение через Admin API |

Логи не должны содержать access token, OAuth client secret, пароли, `RESEARCH_API_TOKEN` или Finam
secret. Верификатор пишет только общий факт отказа.

## Обновление и откат

Обновление:

1. CI должен пройти полностью.
2. В Coolify выбрать Deploy latest commit.
3. Проверить health, OAuth metadata, `401` без токена и авторизованный `tools/call`.
4. Funnel не перенастраивать, если локальный порт остаётся `8765`.

Откат:

1. В Coolify выбрать предыдущий успешный deployment/commit и Redeploy.
2. Не удалять volume `keycloak-data`.
3. Проверить те же пять HTTPS endpoint и авторизованный tool call.
4. При проблеме безопасности сначала выключить только Funnel: `sudo tailscale funnel off`, не
   останавливая Finam Collector.

## Резервное копирование

Keycloak PostgreSQL содержит пользователей и OAuth-конфигурацию. Настроить в Coolify ежедневный
`pg_dump` на существующий USB-накопитель и хранить минимум 7 копий. Проверять восстановление в
отдельный временный контейнер. Секреты Coolify экспортировать только в зашифрованное хранилище.

## Правила безопасности

- Funnel публикует только gateway `8765`.
- MCP допускает только шесть read-only tools и только два SI symbol.
- Лимит свечей — 500; HTTP timeout — 10 секунд; rate limit — 60 запросов в минуту на subject.
- Нет прямого подключения к БД, торговых методов или доступа к брокерским токенам.
- Нельзя называть последнюю минутную свечу биржевым тиком или гарантированно real-time ценой.
- Нельзя удалять Keycloak volume ради обновления realm без отдельного проверенного backup.

