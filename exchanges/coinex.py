import asyncio

from exchanges.http_polling import HttpPollingOrderBookConnection, build_url, chunk_symbols, http_get_json, normalize_levels


class CoinexOrderBookConnection(HttpPollingOrderBookConnection):
    async def fetch_symbol_orderbook(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {
                "market": symbol,
                "limit": self.exchange_settings["depth_limit"],
                "merge": self.exchange_settings["price_interval"],
            },
        )
        payload = await http_get_json(url)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        return normalize_levels(data.get("asks", [])), normalize_levels(data.get("bids", []))


class CoinexExchangeStreamer:
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
                from exchanges.coinex_ws import CoinexWsOrderBookConnection
                connection = CoinexWsOrderBookConnection(
                    self.exchange_settings, self.store, index + 1, group
                )
            else:
                connection = CoinexOrderBookConnection(
                    self.exchange_settings, self.store, index + 1, group, self.semaphore
                )
            tasks.append(asyncio.create_task(connection.run_forever()))
        return tasks
