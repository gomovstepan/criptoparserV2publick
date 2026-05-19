"""WebSocket-коннектор Binance: snapshot + depth@100ms delta stream."""

from exchanges.http_polling import build_url, http_get_json, normalize_levels
from exchanges.websocket_base import WebSocketOrderBookConnection


class BinanceWsOrderBookConnection(WebSocketOrderBookConnection):
    """Binance WebSocket orderbook connection через combined streams."""

    def build_ws_url(self):
        streams = "/".join(f"{symbol.lower()}@depth@100ms" for symbol in self.symbols)
        return f"{self.exchange_settings['ws_url']}/{streams}"

    async def send_subscribe(self, ws):
        # Binance combined stream URL не требует явного subscribe
        return

    async def fetch_rest_snapshot(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {
                "symbol": symbol,
                "limit": self.exchange_settings["orderbook_depth"],
            },
        )
        payload = await http_get_json(url)
        if not isinstance(payload, dict):
            return None
        asks = normalize_levels(payload.get("asks", []))
        bids = normalize_levels(payload.get("bids", []))
        seq = payload.get("lastUpdateId")
        return asks, bids, seq

    def extract_updates(self, message):
        # Combined stream оборачивает в {"stream": ..., "data": ...}
        payload = message.get("data", message)
        symbol = payload.get("s")
        if not symbol:
            return []
        return [{
            "symbol": symbol,
            "type": "delta",
            "asks": normalize_levels(payload.get("a", [])),
            "bids": normalize_levels(payload.get("b", [])),
            "sequence": payload.get("u"),
        }]
