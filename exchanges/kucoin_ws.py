"""WebSocket-коннектор KuCoin: snapshot + level2 delta stream."""

import json

from exchanges.http_polling import build_url, chunk_symbols, http_get_json, http_post_json, normalize_levels
from exchanges.websocket_base import WebSocketOrderBookConnection


class KucoinWsOrderBookConnection(WebSocketOrderBookConnection):
    """KuCoin WebSocket orderbook connection через public bullet token."""

    def __init__(self, exchange_settings, store, connection_id, symbols):
        super().__init__(exchange_settings, store, connection_id, symbols)
        self._resolved_ws_url = None

    async def _resolve_ws_url(self):
        """Получает bullet token и endpoint через REST. При ошибке fallback на сконфигурированный URL."""
        if self._resolved_ws_url:
            return self._resolved_ws_url

        endpoint = None
        token = None

        try:
            payload = await http_post_json(self.exchange_settings["bullet_public_url"], data={})
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            token = data.get("token")
            servers = data.get("instanceServers", [])
            endpoint = servers[0].get("endpoint") if servers else None
        except Exception as error:
            self.logger.warning("KuCoin bullet-public failed (%s), using configured ws_url as fallback", error)

        if not endpoint:
            endpoint = self.exchange_settings.get("ws_url", "wss://ws-api-spot.kucoin.com")

        if token:
            connect_id = f"cpv2-{self.connection_id}"
            self._resolved_ws_url = f"{endpoint}?token={token}&connectId={connect_id}"
        else:
            # Fallback: используем endpoint как есть (возможно пользователь уже встроил токен)
            self._resolved_ws_url = endpoint

        return self._resolved_ws_url

    async def run_forever(self):
        """Переопределяем для разрешения WS URL перед входом в цикл."""
        try:
            await self._resolve_ws_url()
        except Exception as error:
            self.logger.error("KuCoin WS URL resolution failed: %s", error)
            # Продолжаем — при reconnect base class попробует снова
        await super().run_forever()

    def build_ws_url(self):
        return self._resolved_ws_url or self.exchange_settings.get("ws_url", "")

    async def send_subscribe(self, ws):
        for symbol in self.symbols:
            await ws.send(
                json.dumps(
                    {
                        "id": f"{self.connection_id}-{symbol}",
                        "type": "subscribe",
                        "topic": f"/market/level2:{symbol}",
                        "response": True,
                    }
                )
            )

    async def handle_system_message(self, ws, message):
        if message.get("type") == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            return True
        # welcome, ack, pong, subscribe ack — игнорируем
        if message.get("type") in {"welcome", "ack", "pong", "error"}:
            if message.get("type") == "error":
                self.logger.warning("KuCoin WS error message: %s", message)
            return True
        return message.get("type") != "message"

    async def fetch_rest_snapshot(self, symbol):
        url = build_url(self.exchange_settings["rest_orderbook_url"], {"symbol": symbol})
        payload = await http_get_json(url)
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        asks = normalize_levels(data.get("asks", []))
        bids = normalize_levels(data.get("bids", []))
        seq = data.get("sequence")
        return asks, bids, seq

    def extract_updates(self, message):
        topic = message.get("topic", "")
        if "/market/level2:" not in topic:
            return []
        symbol = topic.split(":", 1)[1]
        data = message.get("data", {})

        asks = []
        bids = []
        changes = data.get("changes", {})
        asks.extend(normalize_levels(changes.get("asks", [])))
        bids.extend(normalize_levels(changes.get("bids", [])))

        if data.get("asks") or data.get("bids"):
            asks = normalize_levels(data.get("asks", [])) or asks
            bids = normalize_levels(data.get("bids", [])) or bids

        return [{
            "symbol": symbol,
            "type": "delta",
            "asks": asks,
            "bids": bids,
            "sequence": data.get("sequenceEnd") or data.get("sequence"),
            "exchange_ts": data.get("time"),
        }]
