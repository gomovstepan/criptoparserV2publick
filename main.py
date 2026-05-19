import asyncio
import logging

import redis.asyncio as redis

from common.config import load_settings
from common.logging_config import configure_logging
from common.redis_store import RedisOrderBookStore
from exchanges.binance import BinanceExchangeStreamer
from exchanges.bybit import BybitExchangeStreamer
from exchanges.bitget import BitgetExchangeStreamer
from exchanges.gateio import GateIoExchangeStreamer
from exchanges.coinex import CoinexExchangeStreamer
from exchanges.kucoin import KucoinExchangeStreamer
from exchanges.bingx import BingxExchangeStreamer


STREAMER_BUILDERS = {
    "bybit": BybitExchangeStreamer,
    "binance": BinanceExchangeStreamer,
    "kucoin": KucoinExchangeStreamer,
    "gateio": GateIoExchangeStreamer,
    "bitget": BitgetExchangeStreamer,
    "coinex": CoinexExchangeStreamer,
    "bingx": BingxExchangeStreamer,
}


async def main():
    """Точка входа стримера: поднимает задачи по биржам и держит их запущенными."""
    settings = load_settings()
    configure_logging(settings)
    backend_logger = logging.getLogger("backend")
    redis_logger = logging.getLogger("redis")

    redis_client = redis.from_url(settings["redis"]["url"])
    try:
        await redis_client.ping()
        redis_logger.info("Redis connection established for streamer.")
    except Exception as error:
        redis_logger.error("Redis connection failed for streamer: %s", error)
        raise

    store = RedisOrderBookStore(redis_client, settings["redis"])
    # tasks — список фоновых async-задач стримеров по всем выбранным биржам.
    tasks = []
    backend_logger.info("Streamer startup with exchanges=%s", ",".join(settings["exchanges"]))

    try:
        for exchange_name in settings["exchanges"]:
            builder_class = STREAMER_BUILDERS.get(exchange_name)
            exchange_settings = settings["exchange_configs"].get(exchange_name)

            if builder_class is None:
                backend_logger.error(
                    "Exchange %s configured in EXCHANGES but streamer is not implemented.",
                    exchange_name,
                )
                continue

            if exchange_settings is None:
                backend_logger.error(
                    "Exchange %s has no loaded configuration.",
                    exchange_name,
                )
                continue

            builder = builder_class(exchange_settings, store)
            tasks.extend(builder.build_tasks())

        if not tasks:
            raise RuntimeError("Не создано ни одной задачи стримера. Проверь EXCHANGES и настройки бирж.")

        await asyncio.gather(*tasks)
    finally:
        backend_logger.info("Streamer shutdown: cancelling %s tasks.", len(tasks))
        for task in tasks:
            task.cancel()
        await redis_client.aclose()
        redis_logger.info("Redis connection closed for streamer.")


if __name__ == "__main__":
    asyncio.run(main())
