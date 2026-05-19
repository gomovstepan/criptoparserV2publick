"""Базовый WebSocket-коннектор книги ордеров: snapshot bootstrap + дельты + recovery через REST."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosed


class WebSocketOrderBookConnection:
    """
    Базовый класс для WebSocket-стриминга стакана.

    Жизненный цикл:
    1. REST bootstrap — получаем полный snapshot для каждого символа.
    2. WebSocket connect + subscribe.
    3. Обработка incoming messages: snapshot/delta → apply_update.
    4. Sequence validation — при gap вызываем recover_symbol() через REST.
    5. Reconnect с фиксированной задержкой при ошибках.
    """

    def __init__(self, exchange_settings, store, connection_id, symbols):
        self.exchange_settings = exchange_settings
        self.store = store
        self.connection_id = connection_id
        self.symbols = list(symbols)
        self.logger = logging.getLogger(f"exchanges.{self.exchange_settings['exchange_name']}")
        self.reconnects = 0
        self.last_message_recv_ts = None
        self.state = {symbol: {"ready": False, "last_sequence": None} for symbol in self.symbols}

    async def run_forever(self):
        """Основной цикл: bootstrap → WS session → reconnect при ошибке."""
        reconnect_delay = self.exchange_settings["reconnect_delay_seconds"]

        while True:
            if not self.symbols:
                self.logger.error("No symbols available for ws connection_id=%s.", self.connection_id)
                await asyncio.sleep(reconnect_delay)
                continue

            try:
                await self.bootstrap_from_rest()
                await self._run_ws_session()
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed as error:
                self.reconnects += 1
                self.logger.warning(
                    "WS closed connection_id=%s reconnects=%s: code=%s reason=%s. Retry in %ss.",
                    self.connection_id,
                    self.reconnects,
                    error.rcvd.code if error.rcvd else None,
                    error.reason,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
            except Exception as error:
                self.reconnects += 1
                self.logger.error(
                    "WS error connection_id=%s reconnects=%s: %s. Retry in %ss.",
                    self.connection_id,
                    self.reconnects,
                    error,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)

    async def bootstrap_from_rest(self):
        """Получает начальный snapshot через REST для всех символов."""
        for symbol in self.symbols:
            try:
                snapshot = await self.fetch_rest_snapshot(symbol)
                if snapshot is None:
                    continue
                asks, bids, sequence = snapshot
                await self.store.write_orderbook(
                    exchange_name=self.exchange_settings["exchange_name"],
                    symbol=symbol,
                    message_type="snapshot",
                    asks=asks,
                    bids=bids,
                )
                self.state[symbol]["ready"] = True
                self.state[symbol]["last_sequence"] = sequence
            except Exception as e:
                self.logger.error(
                    "REST bootstrap failed for %s %s: %s",
                    self.exchange_settings["exchange_name"],
                    symbol,
                    e,
                )

    async def _run_ws_session(self):
        """Открывает WebSocket, подписывается и слушает сообщения."""
        ws_url = self.build_ws_url()
        ping_interval = self.exchange_settings.get("ws_ping_interval_seconds", 20)
        ping_timeout = self.exchange_settings.get("ws_ping_timeout_seconds", 20)

        self.logger.info(
            "WS connecting connection_id=%s url=%s symbols=%s",
            self.connection_id,
            ws_url,
            self.symbols,
        )

        async with websockets.connect(
            ws_url,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        ) as ws:
            self.logger.info("WS connected connection_id=%s", self.connection_id)
            try:
                await self.send_subscribe(ws)
            except (ConnectionClosed, AttributeError):
                return

            async for raw_message in ws:
                self.last_message_recv_ts = self.now_utc_ms()
                message = self.decode_message(raw_message)
                if message is None:
                    continue

                try:
                    if await self.handle_system_message(ws, message):
                        continue
                except (ConnectionClosed, AttributeError):
                    break

                updates = self.extract_updates(message)
                for update in updates:
                    await self.apply_update(update)

    async def apply_update(self, update):
        """Применяет одно обновление (snapshot или delta) с валидацией sequence."""
        symbol = update["symbol"]
        update_type = update.get("type", "delta")
        asks = update.get("asks", [])
        bids = update.get("bids", [])
        sequence = update.get("sequence")

        if symbol not in self.state:
            return

        last_sequence = self.state[symbol]["last_sequence"]
        if not self.is_sequence_valid(symbol, update_type, sequence, last_sequence):
            self.logger.warning(
                "Sequence gap detected exchange=%s symbol=%s last=%s incoming=%s; recovering via REST.",
                self.exchange_settings["exchange_name"],
                symbol,
                last_sequence,
                sequence,
            )
            await self.recover_symbol(symbol)
            return

        try:
            await self.store.write_orderbook(
                exchange_name=self.exchange_settings["exchange_name"],
                symbol=symbol,
                message_type=update_type,
                asks=asks,
                bids=bids,
            )
        except Exception as e:
            self.logger.error(
                "Redis write failed during WS update %s %s: %s",
                self.exchange_settings["exchange_name"],
                symbol,
                e,
            )
            return

        self.state[symbol]["ready"] = True
        self.state[symbol]["last_sequence"] = sequence if sequence is not None else last_sequence

    async def recover_symbol(self, symbol):
        """Экстренное восстановление через REST snapshot при sequence gap."""
        try:
            snapshot = await self.fetch_rest_snapshot(symbol)
            if snapshot is None:
                return
            asks, bids, sequence = snapshot
            await self.store.write_orderbook(
                exchange_name=self.exchange_settings["exchange_name"],
                symbol=symbol,
                message_type="snapshot",
                asks=asks,
                bids=bids,
            )
            self.state[symbol]["ready"] = True
            self.state[symbol]["last_sequence"] = sequence
        except Exception as e:
            self.logger.error(
                "REST recovery failed for %s %s: %s",
                self.exchange_settings["exchange_name"],
                symbol,
                e,
            )

    def decode_message(self, raw_message):
        """Декодирует raw WS message в dict. Поддерживает gzip/deflate бинарные фреймы."""
        if isinstance(raw_message, (bytes, bytearray)):
            raw_bytes = bytes(raw_message)
            try:
                raw_message = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raw_message = self._decode_binary_payload(raw_bytes)
                if raw_message is None:
                    self.logger.debug("Skip undecodable binary frame len=%s", len(raw_bytes))
                    return None
        try:
            return json.loads(raw_message)
        except json.JSONDecodeError:
            self.logger.debug("Skip non-JSON WS message: %s", raw_message[:200])
            return None

    def _decode_binary_payload(self, raw_bytes):
        """Пробует декодировать бинарный payload (gzip/deflate/plain-bytes)."""
        import gzip
        import zlib

        if raw_bytes.startswith(b"\x1f\x8b"):
            try:
                return gzip.decompress(raw_bytes).decode("utf-8")
            except Exception:
                return None

        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
            try:
                return zlib.decompress(raw_bytes, wbits=wbits).decode("utf-8")
            except Exception:
                continue

        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def now_utc_ms(self):
        """Возвращает текущее UTC время в миллисекундах."""
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def is_sequence_valid(self, symbol, update_type, sequence, last_sequence):
        """
        Проверяет валидность sequence.
        snapshot всегда валиден; delta требует sequence >= last_sequence.
        """
        if update_type == "snapshot" or sequence is None or last_sequence is None:
            return True
        return int(sequence) >= int(last_sequence)

    # --- Abstract methods ---

    def build_ws_url(self):
        """Возвращает WebSocket URL для подключения."""
        return self.exchange_settings["ws_url"]

    async def handle_system_message(self, ws, message):
        """Обрабатывает системные сообщения (ping/pong и т.д.). Возвращает True если сообщение обработано."""
        return False

    async def send_subscribe(self, ws):
        """Отправляет сообщение подписки на символы."""
        raise NotImplementedError

    async def fetch_rest_snapshot(self, symbol):
        """
        Получает snapshot через REST API.
        Возвращает (asks, bids, sequence) или None.
        """
        raise NotImplementedError

    def extract_updates(self, message):
        """
        Извлекает список обновлений из WS-сообщения.
        Каждое обновление: {"symbol": ..., "type": ..., "asks": ..., "bids": ..., "sequence": ...}
        """
        raise NotImplementedError
