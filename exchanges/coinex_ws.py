"""WebSocket-коннектор CoinEx: snapshot + depth.update delta stream."""

import json

from exchanges.http_polling import build_url, chunk_symbols, http_get_json, normalize_levels
from exchanges.websocket_base import WebSocketOrderBookConnection


class CoinexWsOrderBookConnection(WebSocketOrderBookConnection):
    """CoinEx WebSocket orderbook connection через public stream."""

    async def send_subscribe(self, ws):
        """CoinEx требует depth.subscribe_multi: одна подписка заменяет все предыдущие."""
        params = [
            [
                symbol,
                self.exchange_settings["depth_limit"],
                self.exchange_settings["price_interval"],
                True,
            ]
            for symbol in self.symbols
        ]
        await ws.send(
            json.dumps(
                {
                    "method": "depth.subscribe_multi",
                    "params": params,
                    "id": self.connection_id,
                }
            )
        )

    async def handle_system_message(self, ws, message):
        method = message.get("method")
        if method == "depth.subscribe_multi":
            return True
        if method == "server.ping":
            await ws.send(json.dumps({"method": "server.pong", "params": [], "id": None}))
            return True
        if message.get("error") is not None:
            self.logger.warning("CoinEx WS error message: %s", message)
            return True
        return method is None

    async def fetch_rest_snapshot(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {
                "market": symbol,
                "limit": self.exchange_settings["depth_limit"],
                "merge": self.exchange_settings["price_interval"],
            },
        )
        payload = await http_get_json(url)
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        asks = normalize_levels(data.get("asks", []))
        bids = normalize_levels(data.get("bids", []))
        seq = data.get("time")
        return asks, bids, seq

    def extract_updates(self, message):
        if message.get("method") != "depth.update":
            return []

        params = message.get("params", [])
        if len(params) < 3:
            return []

        is_snapshot = bool(params[0])
        data = params[1]
        symbol = params[2]
        return [{
            "symbol": symbol,
            "type": "snapshot" if is_snapshot else "delta",
            "asks": normalize_levels(data.get("asks", [])),
            "bids": normalize_levels(data.get("bids", [])),
            "sequence": data.get("time"),
            "exchange_ts": None,
        }]
