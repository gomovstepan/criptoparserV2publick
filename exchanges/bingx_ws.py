"""WebSocket-коннектор BingX: snapshot + depth100 delta stream."""

import json

from exchanges.http_polling import build_url, chunk_symbols, http_get_json, normalize_levels
from exchanges.websocket_base import WebSocketOrderBookConnection


class BingxWsOrderBookConnection(WebSocketOrderBookConnection):
    """BingX WebSocket orderbook connection через public stream."""

    async def send_subscribe(self, ws):
        for symbol in self.symbols:
            await ws.send(
                json.dumps(
                    {
                        "id": f"{self.connection_id}-{symbol}",
                        "reqType": "sub",
                        "dataType": f"{symbol}@depth100",
                    }
                )
            )

    async def handle_system_message(self, ws, message):
        if message.get("ping") is not None:
            await ws.send(json.dumps({"pong": message.get("ping")}))
            return True
        # subscribe-ack и прочие системные сообщения
        if message.get("dataType") is None:
            return True
        return False

    async def fetch_rest_snapshot(self, symbol):
        url = build_url(
            self.exchange_settings["rest_orderbook_url"],
            {"symbol": symbol, "limit": self.exchange_settings["orderbook_depth"]},
        )
        payload = await http_get_json(url)
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        asks = normalize_levels(data.get("asks", []))
        bids = normalize_levels(data.get("bids", []))
        seq = data.get("u")
        return asks, bids, seq

    def extract_updates(self, message):
        data_type = message.get("dataType", "")
        if "@depth" not in data_type:
            return []
        symbol = data_type.split("@", 1)[0]
        data = message.get("data", {})
        return [{
            "symbol": symbol,
            "type": "delta",
            "asks": normalize_levels(data.get("asks", [])),
            "bids": normalize_levels(data.get("bids", [])),
            "sequence": data.get("u"),
            "exchange_ts": data.get("E") or message.get("ts"),
        }]
