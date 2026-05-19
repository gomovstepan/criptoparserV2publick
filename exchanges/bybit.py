import asyncio

from exchanges.http_polling import HttpPollingOrderBookConnection, build_url, chunk_symbols, http_get_json, normalize_levels


class BybitOrderBookConnection(HttpPollingOrderBookConnection):
    async def fetch_symbol_orderbook(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {
                "category": self.exchange_settings["category"],
                "symbol": symbol,
                "limit": self.exchange_settings["orderbook_depth"],
            },
        )
        payload = await http_get_json(url)
        data = payload.get("result", {}) if isinstance(payload, dict) else {}
        return normalize_levels(data.get("a", [])), normalize_levels(data.get("b", []))


class BybitExchangeStreamer:
    def __init__(self, exchange_settings, store):
        self.exchange_settings = exchange_settings
        self.store = store
        self.semaphore = asyncio.Semaphore(exchange_settings.get("max_concurrent_requests", 10))

    def build_tasks(self):
        symbol_groups = chunk_symbols(
            self.exchange_settings["symbols"],
            self.exchange_settings["max_symbols_per_connection"],
        )

        use_ws = self.exchange_settings.get("use_websocket", False)
        tasks = []
        for index, group in enumerate(symbol_groups):
            if use_ws:
                from exchanges.bybit_ws import BybitWsOrderBookConnection
                connection = BybitWsOrderBookConnection(
                    self.exchange_settings, self.store, index + 1, group
                )
            else:
                connection = BybitOrderBookConnection(
                    self.exchange_settings, self.store, index + 1, group, self.semaphore
                )
            tasks.append(asyncio.create_task(connection.run_forever()))
        return tasks
