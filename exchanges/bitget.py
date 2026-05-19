import asyncio

from exchanges.http_polling import HttpPollingOrderBookConnection, build_url, chunk_symbols, http_get_json, normalize_levels


class BitgetOrderBookConnection(HttpPollingOrderBookConnection):
    async def fetch_symbol_orderbook(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {
                "symbol": symbol,
                "type": self.exchange_settings["book_type"],
                "limit": self.exchange_settings["orderbook_depth"],
            },
        )
        payload = await http_get_json(url)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}

        if isinstance(data, dict):
            frame = data
        elif isinstance(data, list):
            frame = data[0] if data else {}
        else:
            frame = {}

        return normalize_levels(frame.get("asks", [])), normalize_levels(frame.get("bids", []))


class BitgetExchangeStreamer:
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
                from exchanges.bitget_ws import BitgetWsOrderBookConnection
                connection = BitgetWsOrderBookConnection(
                    self.exchange_settings, self.store, index + 1, group
                )
            else:
                connection = BitgetOrderBookConnection(
                    self.exchange_settings, self.store, index + 1, group, self.semaphore
                )
            tasks.append(asyncio.create_task(connection.run_forever()))
        return tasks
