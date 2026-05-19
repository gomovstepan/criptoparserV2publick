import asyncio
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal


class HttpPollingOrderBookConnection:
    """Базовый HTTP-poller книги ордеров для группы символов."""

    def __init__(self, exchange_settings, store, connection_id, symbols, semaphore=None):
        self.exchange_settings = exchange_settings
        self.store = store
        self.connection_id = connection_id
        self.symbols = list(symbols)
        self.semaphore = semaphore
        self.logger = logging.getLogger(f"exchanges.{self.exchange_settings['exchange_name']}")

    async def run_forever(self):
        reconnect_delay = self.exchange_settings["reconnect_delay_seconds"]
        poll_interval = self.exchange_settings["poll_interval_seconds"]

        while True:
            if not self.symbols:
                self.logger.error("No symbols available for connection_id=%s.", self.connection_id)
                await asyncio.sleep(reconnect_delay)
                continue

            start = time.monotonic()
            try:
                await self.poll_once()
            except Exception as error:
                self.logger.error(
                    "Polling error on connection_id=%s: %s. Retry in %ss.",
                    self.connection_id,
                    error,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                continue

            elapsed = time.monotonic() - start
            sleep_time = poll_interval - elapsed
            if sleep_time <= 0:
                self.logger.warning(
                    "poll_once took %.2fs on connection_id=%s, exceeding poll_interval=%.2fs. "
                    "Consider reducing symbols per connection or increasing max_concurrent_requests.",
                    elapsed,
                    self.connection_id,
                    poll_interval,
                )
                sleep_time = 0.01  # минимальная пауза

            await asyncio.sleep(sleep_time)

    async def poll_once(self):
        async def _fetch_and_write(symbol):
            try:
                if self.semaphore is not None:
                    async with self.semaphore:
                        asks, bids = await self.fetch_symbol_orderbook(symbol)
                else:
                    asks, bids = await self.fetch_symbol_orderbook(symbol)
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    self.logger.error(
                        "HTTP client error %s on connection_id=%s symbol=%s: %s. "
                        "Request will not be retried for this symbol.",
                        e.code,
                        self.connection_id,
                        symbol,
                        e.read().decode("utf-8", errors="replace")[:200],
                    )
                else:
                    self.logger.error(
                        "HTTP server error %s on connection_id=%s symbol=%s: %s. Retry next tick.",
                        e.code,
                        self.connection_id,
                        symbol,
                        e.read().decode("utf-8", errors="replace")[:200],
                    )
                return
            except Exception as error:
                self.logger.error(
                    "Orderbook fetch failed connection_id=%s symbol=%s: %s. Retry next tick.",
                    self.connection_id,
                    symbol,
                    error,
                )
                return

            if not asks or not bids:
                return

            try:
                await self.store.write_orderbook(
                    exchange_name=self.exchange_settings["exchange_name"],
                    symbol=symbol,
                    message_type="snapshot",
                    asks=asks,
                    bids=bids,
                )
            except Exception as error:
                self.logger.error(
                    "Redis write failed connection_id=%s symbol=%s: %s",
                    self.connection_id,
                    symbol,
                    error,
                )

        await asyncio.gather(*[_fetch_and_write(symbol) for symbol in self.symbols])

    async def fetch_symbol_orderbook(self, symbol):
        raise NotImplementedError


def http_get_json(url, timeout=20):
    def _request():
        req = urllib.request.Request(
            url=url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "CriptoParserV2/1.0",
            },
        )
        ssl_context = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
            return json.loads(response.read().decode("utf-8"), parse_float=Decimal)

    return asyncio.to_thread(_request)


def http_post_json(url, data=None, timeout=20):
    """Выполняет POST-запрос с JSON body и возвращает распарсенный JSON."""
    def _request():
        body = json.dumps(data).encode("utf-8") if data is not None else b"{}"
        req = urllib.request.Request(
            url=url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "CriptoParserV2/1.0",
            },
        )
        ssl_context = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
            return json.loads(response.read().decode("utf-8"), parse_float=Decimal)

    return asyncio.to_thread(_request)


def build_url(base_url, query_params):
    return f"{base_url}?{urllib.parse.urlencode(query_params)}"


def strip_trailing_zeros(value):
    """Убирает trailing zeros из строкового представления числа (0.24810000 → 0.2481)."""
    s = str(value)
    if "." not in s:
        return s
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def normalize_levels(levels):
    """Приводит уровни книги к формату [[price, volume], ...]."""
    normalized = []

    if not isinstance(levels, list):
        return normalized

    for item in levels:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            normalized.append([strip_trailing_zeros(item[0]), str(item[1])])
            continue

        if isinstance(item, dict):
            price = item.get("price")
            if price is None:
                price = item.get("p")
            volume = item.get("amount")
            if volume is None:
                volume = item.get("volume")
            if volume is None:
                volume = item.get("size")
            if volume is None:
                volume = item.get("qty")
            if volume is None:
                volume = item.get("q")
            if price is not None and volume is not None:
                normalized.append([strip_trailing_zeros(price), str(volume)])

    return normalized


def chunk_symbols(symbols, chunk_size):
    if chunk_size <= 0:
        raise ValueError("Размер чанка должен быть положительным")
    return [symbols[index:index + chunk_size] for index in range(0, len(symbols), chunk_size)]
