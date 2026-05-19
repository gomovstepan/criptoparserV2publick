"""WebSocket-коннектор Bitget: snapshot + books delta stream."""

import json

from exchanges.http_polling import build_url, chunk_symbols, http_get_json, normalize_levels
from exchanges.websocket_base import WebSocketOrderBookConnection


class BitgetWsOrderBookConnection(WebSocketOrderBookConnection):
    """Bitget WebSocket orderbook connection через public v2 stream."""

    def _get_channel_name(self):
        """Bitget v2 spot WS поддерживает только books5, books15 или books (full)."""
        depth = self.exchange_settings.get("orderbook_depth", 200)
        if depth <= 5:
            return "books5"
        if depth <= 15:
            return "books15"
        return "books"

    async def send_subscribe(self, ws):
        channel = self._get_channel_name()
        args = [
            {
                "instType": "SPOT",
                "channel": channel,
                "instId": symbol,
            }
            for symbol in self.symbols
        ]
        await ws.send(json.dumps({"op": "subscribe", "args": args}))

    async def handle_system_message(self, ws, message):
        if isinstance(message, dict):
            if message.get("event") in {"subscribe", "error"}:
                if message.get("event") == "error":
                    self.logger.warning("Bitget WS error message: %s", message)
                return True
        if message == "pong":
            return True
        return False

    async def fetch_rest_snapshot(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {
                "symbol": symbol,
                "type": self.exchange_settings["book_type"],
                "limit": self.exchange_settings["orderbook_depth"],
            },
        )
        payload = await http_get_json(url)
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        if isinstance(data, list) and data:
            frame = data[0]
        elif isinstance(data, dict):
            frame = data
        else:
            frame = {}
        asks = normalize_levels(frame.get("asks", []))
        bids = normalize_levels(frame.get("bids", []))
        seq = frame.get("seq")
        return asks, bids, seq

    def extract_updates(self, message):
        data = message.get("data", [])
        arg = message.get("arg", {})
        if not data:
            return []
        frame = data[0]
        symbol = frame.get("instId") or arg.get("instId")
        if not symbol:
            return []
        return [{
            "symbol": symbol,
            "type": "snapshot" if message.get("action") == "snapshot" else "delta",
            "asks": normalize_levels(frame.get("asks", [])),
            "bids": normalize_levels(frame.get("bids", [])),
            "sequence": frame.get("seq"),
            "exchange_ts": frame.get("ts"),
        }]
