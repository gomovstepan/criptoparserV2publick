import asyncio
import logging

import redis.asyncio as redis
from fastapi import FastAPI, Header, HTTPException
from redis.exceptions import RedisError

from backend.history_store import ArbitrageHistoryStore
from backend.service import BackendService
from common.config import load_settings
from common.logging_config import configure_logging
from common.redis_store import RedisOrderBookStore
from common.runtime_settings import RuntimeSettingsStore


def create_app() -> FastAPI:
    """Application factory: создаёт и настраивает FastAPI-приложение."""
    settings = load_settings()
    configure_logging(settings)

    backend_logger = logging.getLogger("backend")
    redis_logger = logging.getLogger("redis")

    redis_client = redis.from_url(settings["redis"]["url"])
    store = RedisOrderBookStore(redis_client, settings["redis"])
    history_store = ArbitrageHistoryStore(
        db_path=settings["backend"]["history_db_path"],
        max_records=settings["backend"]["history_limit"],
    )
    runtime_settings_store = RuntimeSettingsStore(
        redis_client=redis_client,
        redis_key=settings["redis"].get("runtime_settings_key", "runtime:settings"),
        file_path=settings["backend"].get("runtime_settings_path", "/data/runtime_settings.json"),
        static_settings=settings,
    )
    service = BackendService(store, settings, history_store, runtime_settings_store)

    app = FastAPI(title="Arbitrage HTTP API")

    @app.on_event("startup")
    async def on_startup():
        try:
            await redis_client.ping()
            redis_logger.info("Redis connection established for backend.")
        except Exception as error:
            redis_logger.error("Redis connection failed for backend: %s", error)
            raise
        backend_logger.info("Backend service startup.")
        await service.start()

    @app.on_event("shutdown")
    async def on_shutdown():
        backend_logger.info("Backend service shutdown started.")
        try:
            await service.stop()
        except Exception:
            pass
        try:
            await redis_client.aclose()
            redis_logger.info("Redis connection closed for backend.")
        except Exception:
            pass

    @app.get("/api/history")
    async def get_history():
        """Возвращает историю арбитража (latest-first). Decimal в ответе сериализуются в string."""
        records = await asyncio.to_thread(history_store.list_records)
        return {
            "data": [
                {
                    "id": record.id,
                    "coin": record.coin,
                    "buy_exchange": record.buy_exchange,
                    "sell_exchange": record.sell_exchange,
                    "start_time": record.start_time,
                    "end_time": record.end_time,
                    "duration_seconds": record.duration_seconds,
                    "max_spread": str(record.max_spread),
                    "net_spread": str(record.net_spread),
                    "fee_total": str(record.fee_total),
                    "slippage_buy": str(record.slippage_buy),
                    "slippage_sell": str(record.slippage_sell),
                    "confidence": str(record.confidence),
                    "data_age_ms": record.data_age_ms,
                }
                for record in records
            ]
        }

    @app.delete("/api/history")
    async def clear_history(x_api_key: str = Header(default="")):
        """Очищает таблицу истории арбитража. Требует API_KEY если он задан в окружении."""
        expected_key = settings.get("api_key", "")
        if expected_key and x_api_key != expected_key:
            raise HTTPException(status_code=403, detail="Invalid API key")
        await asyncio.to_thread(history_store.clear)
        return {"status": "ok"}

    @app.get("/api/raw")
    async def get_raw():
        return service.get_raw_payload()

    @app.get("/api/arbitrage")
    async def get_arbitrage():
        return service.get_arbitrage_payload()

    @app.get("/api/settings")
    async def get_settings():
        """Возвращает текущие runtime-настройки."""
        runtime = await runtime_settings_store.load()
        return {"data": runtime}

    @app.get("/api/settings/schema")
    async def get_settings_schema():
        """Возвращает JSON Schema настроек с типами, default и read_only флагами."""
        return {"data": runtime_settings_store.get_schema()}

    @app.post("/api/settings")
    async def update_settings(payload: dict, x_api_key: str = Header(default="")):
        """Обновляет runtime-настройки в Redis. Требует API_KEY если он задан в окружении."""
        expected_key = settings.get("api_key", "")
        if expected_key and x_api_key != expected_key:
            raise HTTPException(status_code=403, detail="Invalid API key")
        ok, errors = runtime_settings_store.validate(payload)
        if not ok:
            raise HTTPException(status_code=422, detail={"errors": errors})
        await runtime_settings_store.save(payload)
        backend_logger.info("Runtime settings updated via API: %s", payload)
        return {"status": "ok"}

    return app
