"""WebSocket-коннектор Bybit: snapshot + orderbook delta stream."""

import json

from exchanges.http_polling import build_url, http_get_json, normalize_levels
from exchanges.websocket_base import WebSocketOrderBookConnection


class BybitWsOrderBookConnection(WebSocketOrderBookConnection):
    """Bybit WebSocket orderbook connection через public v5 stream."""

    def build_ws_url(self):
        """Bybit spot требует /spot в пути. Добавляем, если отсутствует."""
        url = self.exchange_settings.get("ws_url", "wss://stream.bybit.com/v5/public/spot")
        if not url.rstrip("/").endswith("/spot"):
            url = url.rstrip("/") + "/spot"
        return url

    async def send_subscribe(self, ws):
        depth = self.exchange_settings.get("orderbook_depth", 50)
        args = [f"orderbook.{depth}.{symbol}" for symbol in self.symbols]
        await ws.send(json.dumps({"op": "subscribe", "args": args}))

    async def handle_system_message(self, ws, message):
        if message.get("op") == "ping":
            await ws.send('{"op":"pong"}')
            return True
        # success/_subscription ответы — игнорируем
        if "topic" not in message:
            return True
        return False

    async def fetch_rest_snapshot(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {
                "category": self.exchange_settings.get("category", "spot"),
                "symbol": symbol,
                "limit": self.exchange_settings["orderbook_depth"],
            },
        )
        payload = await http_get_json(url)
        if not isinstance(payload, dict):
            return None
        data = payload.get("result", {})
        asks = normalize_levels(data.get("a", []))
        bids = normalize_levels(data.get("b", []))
        sequence = data.get("u")
        return asks, bids, sequence

    def extract_updates(self, message):
        topic = message.get("topic", "")
        if not topic.startswith("orderbook"):
            return []
        parts = topic.split(".")
        symbol = parts[-1] if len(parts) >= 3 else None
        data = message.get("data", {})
        if not symbol:
            return []
        return [{
            "symbol": symbol,
            "type": "snapshot" if message.get("type") == "snapshot" else "delta",
            "asks": normalize_levels(data.get("a", [])),
            "bids": normalize_levels(data.get("b", [])),
            "sequence": data.get("u") or data.get("seq"),
        }]
