<!-- From: s:\coding\criptoparserV2\AGENTS.md -->
# AGENTS.md — CriptoParserV2

Этот файл описывает архитектуру, конвенции и инструкции по работе с проектом для AI-агентов. Читатель файла не знаком с проектом.

---

## Обзор проекта

**CriptoParserV2** — сервис для сканирования криптовалютного арбитража. Он опрашивает REST API книг ордеров (orderbook) у 7 бирж, сохраняет снапшоты в Redis, агрегирует ASK/BID-уровни до заданного объёма в котируемой валюте, ищет межбиржевые спреды и отдаёт результаты через HTTP API. Также есть уведомления в Telegram и SQLite-хранилище истории событий.

**Ключевые возможности:**
- Расчёт **net spread** с учётом торговых комиссий, slippage (VWAP) и withdrawal fee.
- **Динамические пороги** по Tier-классификации пар (Tier 1–4).
- **Confidence score** для фильтрации ненадёжных сигналов.
- **Runtime Settings UI** — изменение порогов, комиссий и таймингов на лету через frontend.

**Поддерживаемые биржи:** `bybit`, `binance`, `kucoin`, `gateio`, `bitget`, `coinex`, `bingx`.

**Основной язык комментариев и документации:** русский.

---

## Стек технологий

### Backend
- **Python 3.10**
- **FastAPI** — HTTP API (`backend/main.py`)
- **Uvicorn** — ASGI-сервер
- **redis-py (asyncio)** — асинхронный клиент Redis
- **python-dotenv** — загрузка `.env`
- **SQLite3** — хранение истории завершённых арбитражных событий
- **pytest** — тестирование (`pytest.ini` задаёт `asyncio_mode = auto`)
- **Стандартная библиотека:** `asyncio`, `urllib.request`, `ssl`, `json`, `logging`, `sqlite3`, `decimal`, `dataclasses`, `itertools`

### Frontend
- **Vue 3** (Composition API, `<script setup>`)
- **Vite** — сборка и dev-сервер
- Чистый CSS (тёмная тема), без UI-фреймворков
- Нет роутера, нет state management — весь UI в одном компоненте `App.vue`

### Инфраструктура
- **Redis 7** — хранилище orderbook и событий арбитража
- **Docker + Docker Compose** — оркестрация всех сервисов
- **Nginx** — раздача фронтенда и проксирование `/api/` на backend
- **redis-commander** — веб-UI для Redis

---

## Архитектура и поток данных

Проект состоит из трёх независимых runtime-процессов:

1. **Streamer (`main.py`)**
   - Запускает по одному поллеру на каждую активную биржу.
   - Каждый поллер — это набор `HttpPollingOrderBookConnection`, работающих в отдельных asyncio-тасках.
   - Читает REST API бирж и пишет нормализованные снапшоты в Redis через `RedisOrderBookStore.write_orderbook()`.
   - Символы делятся на группы (`chunk_symbols`) по `MAX_SYMBOLS_PER_CONNECTION`; на каждую группу создаётся отдельное соединение.

2. **Backend (`backend/main.py`)**
   - FastAPI-приложение (factory `create_app()`).
   - При старте запускает одну фоновую задачу: `BackendService.run()`.
   - `run()` — единственный цикл backend: читает raw + aggregated снапшоты из Redis, загружает runtime-настройки из Redis, ищет арбитраж с расчётом net spread и confidence, upsert'ит события, обрабатывает их lifecycle (Telegram-отправка / архивация в SQLite) и обновляет HTTP payload — всё последовательно в одном tick'е.
   - Эндпоинты:
     - `GET /api/raw` — сырой снапшот стаканов (best ASK/BID).
     - `GET /api/arbitrage` — активные арбитражные события из Redis.
     - `GET /api/history` — история завершённых событий из SQLite.
     - `DELETE /api/history` — очистка таблицы истории. Если задан `API_KEY`, требует его в заголовке `X-API-Key`.
     - `GET /api/settings` — текущие runtime-настройки.
     - `GET /api/settings/schema` — JSON Schema настроек (типы, default, read_only).
     - `POST /api/settings` — обновление runtime-настроек. Требует `API_KEY` в `X-API-Key`.

3. **Frontend (`frontend/src/App.vue`)**
   - Одностраничное Vue-приложение.
   - Опрашивает `/api/raw`, `/api/arbitrage`, `/api/history` и `/api/settings` через `fetch()`.
   - Четыре вкладки: Raw Data, Arbitrage, History, **Settings**.
   - Во вкладке **Settings** отображается динамическая форма из `/api/settings/schema`. Runtime-поля редактируются, static-поля read-only. Сохранение через `POST /api/settings` с `X-API-Key`.
   - Во вкладке History доступна сортировка по столбцам и кнопка очистки истории.
   - Длительность активных событий обновляется живым таймером (`nowMs`).
   - Временные метки в истории и Telegram отображаются в часовом поясе Владивосток (UTC+10).

### Поток данных
```
Биржи REST API
      ↓
Streamer (main.py) — нормализация символов, запись в Redis Hashes + Sets
      ↓
Backend (backend/main.py) — чтение, агрегация, поиск спредов, управление событиями
      ↓
Frontend — отображение таблиц
      ↓
Telegram — уведомления об арбитраже (Markdown + ссылки на торговые страницы)
      ↓
SQLite — история завершённых событий с retention limit
```

### Нормализация символов
Backend приводит символы к единому виду перед сравнением: uppercase + только буквы/цифры (`normalize_symbol`). Это позволяет сопоставлять `SOLUSDT` (Bybit), `SOL-USDT` (Kucoin/BingX) и `SOL_USDT` (GateIO).

---

## Организация кода

```
.
├── main.py                    # Точка входа streamer: запускает поллеры бирж
├── backend/
│   ├── main.py                # FastAPI factory (create_app) + эндпоинты
│   ├── service.py             # BackendService + TelegramNotifier + ArbitrageOpportunity
│   ├── spread_calculator.py   # NetSpreadCalculator + VWAP/slippage/net_spread
│   └── history_store.py       # SQLite-хранилище завершённых событий (ArbitrageHistoryStore)
├── common/
│   ├── config.py              # Загрузка и валидация .env-конфигурации (load_settings)
│   ├── logging_config.py      # Настройка rotating-логов по компонентам
│   ├── redis_store.py         # DAL для Redis: orderbook + события арбитража
│   └── runtime_settings.py    # RuntimeSettingsStore: Redis + JSON fallback + schema
├── exchanges/
│   ├── http_polling.py        # Базовый класс HttpPollingOrderBookConnection + утилиты
│   ├── binance.py, bybit.py,  # Реализации под каждую биржу
│   ├── kucoin.py, gateio.py,
│   ├── bitget.py, coinex.py,
│   └── bingx.py
├── frontend/
│   ├── src/
│   │   ├── main.js            # Точка входа Vue
│   │   ├── App.vue            # Единственный компонент со всей логикой UI
│   │   └── styles.css         # Глобальные стили
│   ├── Dockerfile             # Multi-stage: build → nginx
│   ├── nginx.conf             # SPA fallback + проксирование /api/
│   ├── package.json           # Vue 3 + Vite
│   └── vite.config.js
└── tests/
    ├── test_backend_logic.py  # Логика арбитража, нормализация, форматирование
    ├── test_backend_logic_advanced.py  # Tiers, confidence, net spread
    ├── test_exchanges.py      # HttpPolling, normalize_levels, chunk_symbols
    ├── test_config.py         # Загрузка конфигурации
    ├── test_redis_store.py    # Redis DAL + агрегация
    ├── test_history_store.py  # SQLite persistence
    ├── test_runtime_settings.py # Runtime settings: Redis, файл, валидация, schema
    ├── test_spread_calculator.py # VWAP, slippage, net_spread
    ├── test_backend_bugs.py   # AST-проверки архитектурных багов
    ├── test_audit_critical.py # Регрессия CRITICAL-проблем (чтение Redis, lifecycle событий)
    ├── test_audit_major.py    # Регрессия MAJOR-проблем (HTTP, Telegram, frontend)
    └── test_audit_minor.py    # Регрессия MINOR-проблем (стиль, AST)
```

---

## Сборка, запуск и тестирование

### Docker Compose (рекомендуемый способ)

```bash
cp .env.example .env
# Отредактируйте .env при необходимости
docker compose up --build -d
```

- Frontend: `http://localhost:8080`
- Backend API: `http://localhost:8000`
- Redis UI: `http://localhost:8081`

### Локальный запуск (без Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Redis должен быть доступен (локально или в Docker)
redis-server --port 6379

# Терминал 1 — streamer
python main.py

# Терминал 2 — backend API
python -m uvicorn backend.main:create_app --factory --host 0.0.0.0 --port 8000

# Терминал 3 — frontend (опционально)
cd frontend
npm install
npm run dev
```

### Команды проверки

```bash
# Синтаксическая проверка Python
python -m py_compile main.py backend/main.py common/config.py common/redis_store.py exchanges/*.py
python -m compileall .

# Проверка конфигурации Docker Compose
docker compose config

# Запуск тестов
pytest

# Запуск конкретного тестового модуля
pytest tests/test_backend_logic.py -v
pytest tests/test_redis_store.py -v
```

---

## Конфигурация и переменные окружения

Вся конфигурация — через переменные окружения, загружаемые из `.env` через `python-dotenv`.

### Глобальные переменные

| Переменная | Назначение |
|------------|-----------|
| `EXCHANGES` | CSV-список активных бирж |
| `REDIS_URL` | Строка подключения к Redis |
| `API_PORT` / `FRONTEND_PORT` / `REDIS_UI_PORT` | Порты сервисов |
| `EXCHANGE_POLL_INTERVAL_SECONDS` | Базовый интервал HTTP-опроса (по умолчанию `1`) |
| `ORDERBOOK_TARGET_VALUE` | Целевая стоимость для агрегации книги (в котируемой валюте) |
| `ORDERBOOK_MAX_LEVELS` | Максимум уровней при агрегации |
| `BACKEND_RENDER_INTERVAL_SECONDS` | Интервал цикла backend |
| `ARBITRAGE_MIN_SPREAD_PERCENT` / `SPREAD_THRESHOLD_PCT` | Fallback-минимальный спред для сигнала арбитража (%) |
| `EVENT_SEND_DELAY_SECONDS` | Задержка перед первой отправкой события в Telegram |
| `EVENT_EXPIRE_SECONDS` | Время без обновлений до закрытия события и архивации в SQLite |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram (опционально) |
| `ARBITRAGE_HISTORY_LIMIT` | Максимальное число записей в SQLite (retention) |
| `ARBITRAGE_HISTORY_DB_PATH` | Путь к файлу SQLite (`/data/arbitrage_history.db` в Docker) |
| `LOG_LEVEL` / `LOG_DIR` | Логирование |
| `API_KEY` | API-ключ для защиты `DELETE /api/history` и `POST /api/settings` (опционально) |
| `RUNTIME_SETTINGS_REDIS_KEY` | Redis Hash ключ для runtime-настроек (`runtime:settings`) |
| `RUNTIME_SETTINGS_PATH` | Путь к JSON-файлу персистентности (`/data/runtime_settings.json`) |
| `CONFIDENCE_MIN` | Минимальный confidence score для отправки в Telegram (`70`) |
| `WITHDRAWAL_FEE_USDT` | Фиксированный withdrawal fee в USDT (`0` для pre-funded) |
| `EXCHANGE_FEE_<EXCHANGE>` | Базовый taker fee по биржам в % (`0.10` или `0.20`) |
| `TIER_1_PAIRS` / `TIER_2_PAIRS` / `TIER_3_PAIRS` / `TIER_4_PAIRS` | CSV-списки пар по ликвидности |
| `TIER_1_THRESHOLD` / `TIER_2_THRESHOLD` / `TIER_3_THRESHOLD` / `TIER_4_THRESHOLD` | Пороги спреда по Tier |

### Redis-шаблоны ключей (обязательные)

| Переменная | Назначение |
|------------|-----------|
| `ORDERBOOK_REDIS_KEY_PREFIX` | Префикс для ключей стаканов |
| `ORDERBOOK_REDIS_SYMBOLS_SET_TEMPLATE` | Шаблон Set со списком символов биржи |
| `ARBITRAGE_EVENTS_REDIS_SET_KEY` | Set с активными арбитражными событиями |
| `ARBITRAGE_EVENT_REDIS_KEY_TEMPLATE` | Шаблон Hash ключа состояния события |
| `RUNTIME_SETTINGS_REDIS_KEY` | Redis Hash ключ для runtime-настроек |

### Переменные по биржам

Каждая биржа имеет свой префикс (`BYBIT_*`, `BINANCE_*`, `KUCOIN_*`, `GATEIO_*`, `BITGET_*`, `COINEX_*`, `BINGX_*`).

Обязательные поля: `REST_ORDERBOOK_URL`, `SYMBOLS`, `RECONNECT_DELAY_SECONDS`, `MAX_SYMBOLS_PER_CONNECTION`.

Опциональные: `POLL_INTERVAL_SECONDS`, `ORDERBOOK_DEPTH`, `MAX_CONCURRENT_REQUESTS` (по умолчанию `10`) и прочие специфичные параметры.

**Важно:** разные биржи используют разный формат символов в URL/API:
- Bybit, Binance, Bitget, Coinex: `SOLUSDT`
- Kucoin, BingX: `SOL-USDT`
- GateIO: `SOL_USDT`

Backend нормализует их для сопоставления.

### Файлы конфигурации

- `.env.example` — базовый шаблон для dev/stage
- `env.production.example` — прод-шаблон (более агрессивные параметры агрегации, другой Redis-хост)

**Никогда не коммитьте реальные токены и секреты.** `.env` исключён из git.

---

## Стиль кода и конвенции

### Python
- Используется **PEP 8** без строгих линтеров.
- Комментарии и docstrings — на **русском языке**.
- Асинхронный код на `asyncio` — все поллеры и Redis-операции async.
- HTTP-запросы к биржам и Telegram делаются через `urllib.request` внутри `asyncio.to_thread()`, а не через aiohttp.
- Финансовые расчёты используют `Decimal` для точности; в JSON-ответах `Decimal` сериализуется в `str`.
- Каждый обменник реализует одинаковый паттерн: класс `*OrderBookConnection` (наследник `HttpPollingOrderBookConnection`) + класс `*ExchangeStreamer` с методом `build_tasks()`.
- Утилиты для polling живут в `exchanges/http_polling.py`: `http_get_json`, `build_url`, `normalize_levels`, `chunk_symbols`.

### JavaScript / Vue
- Composition API (`<script setup>`).
- Весь UI и логика находятся в одном файле `App.vue`.
- Используется `fetch()` без обёрток (axios и т.п.).
- Периодический опрос через `setInterval(..., 1000)`.

### Логирование
- Настроено через `common/logging_config.py`.
- `RotatingFileHandler` (10 МБ, 5 бэкапов).
- Отдельные логгеры и файлы для: `backend`, `arbitrage`, `redis`, `telegram`, `exchanges.<имя_биржи>`.
- Файлы пишутся в `LOG_DIR` (по умолчанию `/app/logs`, в Docker монтируется в `./logs`).

---

## Тестирование

Проект покрыт **pytest** с включённым `asyncio_mode = auto`.

### Структура тестов

- `tests/test_backend_logic.py` — unit-тесты логики арбитража: нормализация символов, разбор пары, расчёт спреда, поиск возможностей, форматирование цен/процентов.
- `tests/test_exchanges.py` — тесты `http_polling.py`: парсинг уровней стакана, chunk-разбиение, построение URL, семафоры concurrency, `http_get_json` (mock urllib).
- `tests/test_config.py` — тесты загрузки конфигурации: CSV-парсинг, значения по умолчанию, legacy-переменные (`SPREAD_THRESHOLD_PCT`), custom `API_KEY`.
- `tests/test_redis_store.py` — тесты DAL Redis: decode/encode, агрегация стакана, pipeline, TTL, чтение событий.
- `tests/test_history_store.py` — тесты SQLite: insert/list, retention limit, WAL mode, дедупликация по UNIQUE constraint.
- `tests/test_backend_bugs.py` — AST-регрессии: проверяет отсутствие module-level инициализации в `backend/main.py`, наличие `dedup_ttl_seconds` в `TelegramNotifier`, избыточные `except` блоки.
- `tests/test_audit_critical.py` — регрессия критичных багов: graceful degradation при ошибке одной биржи, cleanup событий при падении Telegram/SQLite, защита от битого JSON в Redis.
- `tests/test_audit_major.py` — регрессия серьёзных багов: дедупликация Telegram, clock skew, HTTP-статусы, API-ключ frontend, флаг `existed` в upsert.
- `tests/test_audit_minor.py` — регрессия мелких проблем: AST-проверки, парсинг уровней, TTL, transaction pipeline.

### Запуск

```bash
pytest                    # все тесты
pytest -v                 # подробный вывод
pytest tests/test_backend_logic.py -v   # конкретный модуль
```

### Важные особенности

- `test_backend_logic.py` тестирует извлечённые stubs классов, а не импорт из `backend/service.py`, чтобы избежать циклических/сложных зависимостей при импорте.
- `test_backend_bugs.py` использует `ast.parse()` для статического анализа исходников `backend/main.py` и `backend/service.py`.
- В `test_redis_store.py` активно используются `unittest.mock.AsyncMock` и `MagicMock` для эмуляции redis-py.

---

## Безопасность

- Не храните `TELEGRAM_BOT_TOKEN` и прочие секреты в репозитории.
- `.env` и `.env.production` исключены из `.gitignore`.
- В Docker-образе Python-код выполняется от имени root (в `Dockerfile` нет директивы `USER`).
- REST API бирж вызываются через HTTPS с `ssl.create_default_context()`.
- `DELETE /api/history` и `POST /api/settings` защищены опциональным `API_KEY` (передаётся в заголовке `X-API-Key`).
- Runtime settings хранятся в Redis Hash (`runtime:settings`) и персистятся в JSON-файл. Backend и Streamer читают их на каждом тике.
- Fee-конфигурация и tier-пороги могут меняться на лету через `POST /api/settings` или frontend-вкладку Settings.
- **WebSocket** поддерживается для Binance и Bybit (пилот). Переключение HTTP↔WebSocket через `*_USE_WEBSOCKET=true/false` в `.env`. GateIO остаётся на HTTP polling.

---

## Деплой

### Docker Compose (основной способ)
```bash
docker compose up --build -d
```

### Прод-деплой
1. Скопируйте `env.production.example` → `.env.production`.
2. Заполните секреты (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) и хост Redis.
3. Укажите `.env.production` как `env_file` в вашем compose/k8s/CI.

### Сервисы в `docker-compose.yml`

| Сервис | Образ / Build | Назначение |
|--------|---------------|-----------|
| `redis` | `redis:7-alpine` | Хранилище данных |
| `redis-ui` | `rediscommander/redis-commander:latest` | Веб-UI для Redis |
| `streamer` | Build root `Dockerfile` | Поллеры бирж |
| `backend` | Build root `Dockerfile` | FastAPI + бизнес-логика (`create_app` factory) |
| `frontend` | Build `frontend/Dockerfile` | Nginx со статикой Vue |

---

## Известные проблемы и TODO

- **GateIO** — очень долгое время подключения/обновления; наблюдались WebSocket-ошибки `keepalive ping timeout` и проблемы реконнекта по конкретным символам. В текущей версии используется только HTTP polling, но записи в `todo` свидетельствуют о предыдущих экспериментах с WebSocket.
- **Clock skew** — `_is_fresh` сравнивает локальное время Streamer и Backend. При разъезде часов >30с все данные отбраковываются. Полное решение требует перехода на монотонные часы или Redis-based timestamp.
- **Poll interval enforcement** — при больших группах символов (200+) с низким `max_concurrent_requests` фактический интерал опроса может превышать `poll_interval`. Добавлено warning-логирование, но не rate-limiting.

---

## Резюме для агента

- Всегда редактируй `.env.example` и `env.production.example` синхронно при изменении конфигурации.
- При добавлении новой биржи следуй паттерну: создать `exchanges/<name>.py` с `*OrderBookConnection` и `*ExchangeStreamer`, зарегистрировать в `main.py` (`STREAMER_BUILDERS`), добавить переменные окружения в `common/config.py` и `.env.example`.
- Backend и Streamer — два отдельных процесса. Streamer не знает про backend, backend не знает про streamer; они связаны только через Redis.
- Фронтенд — минималистичный Vue. Не добавляй роутер или state management без явной необходимости.
- Все числовые расчёты спредов — через `Decimal`. Не используй `float` для финансовой логики.
- HTTP-запросы к биржам делаются через `urllib.request` в `asyncio.to_thread()`, не через aiohttp/requests.
- Символы разных бирж нормализуются в backend перед сопоставлением.
- `backend/main.py` — только FastAPI factory (`create_app()`). Вся бизнес-логика вынесена в `backend/service.py`.
- При изменении логики backend пиши/обновляй тесты в `tests/test_backend_logic.py`, `tests/test_backend_logic_advanced.py`, `tests/test_backend_bugs.py` и `tests/test_audit_critical.py`.
- При изменении spread calculator пиши/обновляй тесты в `tests/test_spread_calculator.py`.
- При изменении runtime settings пиши/обновляй тесты в `tests/test_runtime_settings.py`.
- При изменении DAL Redis пиши/обновляй тесты в `tests/test_redis_store.py` и `tests/test_audit_critical.py`.
- При изменении Streamer (HTTP polling, WebSocket, обработка ошибок) пиши/обновляй тесты в `tests/test_exchanges.py`, `tests/test_websocket_base.py`, `tests/test_binance_ws.py`, `tests/test_bybit_ws.py` и `tests/test_audit_major.py`.
- При изменении frontend (App.vue) проверь `tests/test_audit_major.py::TestM5FrontendMissingApiKey`.
