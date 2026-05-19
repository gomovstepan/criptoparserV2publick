# CriptoParserV2

Сервис собирает стаканы с нескольких криптобирж через HTTP polling, сохраняет их в Redis, рассчитывает арбитражные возможности и отдает данные во frontend через HTTP API.

## Архитектура

- **streamer (`main.py`)** — опрашивает HTTP API бирж и пишет orderbook в Redis.
- **backend (`backend/main.py`)** — читает Redis, агрегирует ASK/BID, ищет арбитраж и публикует данные в `/api/raw` и `/api/arbitrage`.
- **frontend (`frontend/`)** — Vue-приложение с таблицами сырого стакана и активного арбитража.
- **redis + redis-commander** — хранение и просмотр данных.

## Поддерживаемые биржи

`bybit`, `binance`, `kucoin`, `gateio`, `bitget`, `coinex`, `bingx`.

## Быстрый старт (Docker Compose)

1. Скопируйте env-файл:

```bash
cp .env.example .env
```

2. Запустите сервисы:

```bash
docker compose up --build -d
```

3. Откройте:

- Frontend: `http://localhost:${FRONTEND_PORT}` (по умолчанию `http://localhost:8080`)
- Backend API:
  - `http://localhost:${API_PORT}/api/raw`
  - `http://localhost:${API_PORT}/api/arbitrage`
- Redis UI: `http://localhost:${REDIS_UI_PORT}` (по умолчанию `http://localhost:8081`)

4. Логи:

```bash
docker compose logs -f streamer
docker compose logs -f backend
```

Файловые логи также сохраняются на хост-машине в `./logs` (volume `./logs:/app/logs`):

- `logs/backend.log`
- `logs/arbitrage.log`
- `logs/redis.log`
- `logs/telegram.log`
- `logs/exchanges/<exchange>.log` (например `bybit.log`, `binance.log`, `kucoin.log`)

## Локальный запуск (без Docker)

### 1) Подготовка окружения

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2) Запуск Redis локально

Вариант A (локально установленный Redis):

```bash
redis-server --port 6379
```

Вариант B (через Docker, только Redis):

```bash
docker run --rm -p 6379:6379 redis:7-alpine
```

Если запускаете локальный Redis на `localhost`, в `.env` укажите:

```env
REDIS_URL=redis://localhost:6379/0
```

### 3) Запуск backend-процессов (в разных терминалах)

Терминал №1 — streamer (читает биржи и пишет в Redis):

```bash
source .venv/bin/activate
python main.py
```

Терминал №2 — API backend (FastAPI HTTP):

```bash
source .venv/bin/activate
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### 4) (Опционально) запуск frontend

```bash
cd frontend
npm install
npm run dev
```

### 5) Проверка, что backend поднят

- API: `http://localhost:8000/api/history`
- Raw snapshot API: `http://localhost:8000/api/raw`
- Arbitrage API: `http://localhost:8000/api/arbitrage`

## Переменные окружения

Актуальный список переменных:

- `.env.example` — базовый шаблон для dev/stage.
- `.env.production.example` — рекомендуемый шаблон для прод-стенда.

### Ключевые переменные

- `EXCHANGES` — список активных бирж.
- `ORDERBOOK_TARGET_VALUE`, `ORDERBOOK_MAX_LEVELS` — параметры агрегации книги.
- `ARBITRAGE_MIN_SPREAD_PERCENT` — минимальный спред для сигналов.
- `BACKEND_RENDER_INTERVAL_SECONDS` — интервал цикла backend.
- `EXCHANGE_POLL_INTERVAL_SECONDS` — базовая частота HTTP-опроса бирж (в секундах, по умолчанию `1`).
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — уведомления (опционально).
- `LOG_LEVEL` — уровень логирования (`DEBUG`, `INFO`, `ERROR`).
- `LOG_DIR` — директория файловых логов внутри контейнера (по умолчанию `/app/logs`).

## Прод-конфигурация

1. Скопируйте шаблон:

```bash
cp .env.production.example .env.production
```

2. Заполните секреты (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) и хост Redis.
3. Используйте `.env.production` как `env_file` в деплой-конфигурации (compose/k8s/ci).
4. Не храните реальные токены в git.

## Проверки

```bash
python -m py_compile main.py backend/main.py common/config.py common/redis_store.py exchanges/*.py
python -m compileall .
docker compose config
```
