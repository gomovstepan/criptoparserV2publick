import asyncio

from exchanges.http_polling import HttpPollingOrderBookConnection, build_url, chunk_symbols, http_get_json, normalize_levels


class GateIoOrderBookConnection(HttpPollingOrderBookConnection):
    async def fetch_symbol_orderbook(self, symbol):
        url = build_url(
            self.exchange_settings["rest_url"],
            {
                "currency_pair": symbol,
                "limit": self.exchange_settings["rest_snapshot_limit"],
                "with_id": "true",
            },
        )
        payload = await http_get_json(url)
        return normalize_levels(payload.get("asks", [])), normalize_levels(payload.get("bids", []))


class GateIoExchangeStreamer:
    def __init__(self, exchange_settings, store):
        self.exchange_settings = exchange_settings
        self.store = store
        self.semaphore = asyncio.Semaphore(exchange_settings.get("max_concurrent_requests", 10))

    def build_tasks(self):
        symbol_groups = chunk_symbols(
            self.exchange_settings["symbols"],
            self.exchange_settings["max_symbols_per_connection"],
        )

        tasks = []
        for index, group in enumerate(symbol_groups):
            connection = GateIoOrderBookConnection(self.exchange_settings, self.store, index + 1, group, self.semaphore)
            tasks.append(asyncio.create_task(connection.run_forever()))
        return tasks
