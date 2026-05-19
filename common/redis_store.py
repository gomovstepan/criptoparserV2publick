from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import json

from common.utils import normalize_symbol


class AggregatedSide:
    """Контейнер агрегированного среза стакана по одной стороне (ASK/BID)."""

    def __init__(self, price, volume, value, levels):
        # price — средневзвешенная цена по использованным уровням.
        self.price = price
        # volume — суммарный доступный объём в базовом активе.
        self.volume = volume
        # value — суммарная стоимость объёма в котируемой валюте.
        self.value = value
        # levels — число уровней книги, участвовавших в агрегации.
        self.levels = levels


class RedisOrderBookStore:
    """Слой доступа к Redis для чтения/записи сырых и агрегированных стаканов."""

    def __init__(self, redis_client, redis_settings):
        self.redis = redis_client
        # redis_settings хранит шаблоны ключей и префиксы для нейминга в Redis.
        self.redis_settings = redis_settings
        self.logger = logging.getLogger("redis")

    def symbols_key(self, exchange_name):
        """Формирует ключ Redis Set со списком символов конкретной биржи."""
        template = self.redis_settings["symbols_set_template"]
        return template.format(exchange=exchange_name)

    def side_key(self, exchange_name, symbol, side):
        """Формирует ключ Redis Hash для одной стороны стакана по бирже и символу."""
        prefix = self.redis_settings["key_prefix"]
        return f"{prefix}:{exchange_name}:{normalize_symbol(symbol)}:{side.lower()}"

    def meta_key(self, exchange_name, symbol):
        """Формирует ключ Redis Hash для метаданных стакана (timestamp)."""
        prefix = self.redis_settings["key_prefix"]
        return f"{prefix}:{exchange_name}:{normalize_symbol(symbol)}:meta"

    def arbitrage_events_set_key(self):
        """Возвращает ключ Redis Set со списком активных арбитражных событий."""
        return self.redis_settings["arbitrage_events_set_key"]

    def arbitrage_event_key(self, event_key):
        """Возвращает Redis Hash ключ состояния конкретного арбитражного события."""
        template = self.redis_settings["arbitrage_event_key_template"]
        return template.format(event_key=event_key)

    async def write_orderbook(self, exchange_name, symbol, message_type, asks, bids):
        """Записывает snapshot/delta стакана в Redis с атомарным pipeline + TTL."""
        ask_key = self.side_key(exchange_name, symbol, "ASK")
        bid_key = self.side_key(exchange_name, symbol, "BID")
        meta_key = self.meta_key(exchange_name, symbol)
        pipeline = self.redis.pipeline(transaction=True)

        pipeline.sadd(self.symbols_key(exchange_name), normalize_symbol(symbol))

        if message_type == "snapshot":
            pipeline.delete(ask_key)
            pipeline.delete(bid_key)

        self._queue_levels(pipeline, ask_key, asks)
        self._queue_levels(pipeline, bid_key, bids)

        now_iso = datetime.now(timezone.utc).isoformat()
        pipeline.hset(meta_key, mapping={"updated_at": now_iso})

        ttl_seconds = max(60, int(self.redis_settings.get("ttl_seconds", 60)))
        pipeline.expire(ask_key, ttl_seconds)
        pipeline.expire(bid_key, ttl_seconds)
        pipeline.expire(meta_key, ttl_seconds)

        try:
            await pipeline.execute()
        except Exception as error:
            self.logger.error(
                "Redis pipeline failed for %s %s: %s",
                exchange_name,
                symbol,
                error,
            )
            raise

    def _queue_levels(self, pipeline, redis_key, levels):
        """Готовит операции HSET/HDEL для пакета уровней стакана."""
        updates = {}
        removals = []

        for raw_price, raw_volume in levels:
            try:
                # Убираем trailing zeros чтобы нормализовать ключи (0.24810000 → 0.2481)
                price_str = str(raw_price)
                if "." in price_str:
                    price_str = price_str.rstrip("0").rstrip(".")
                price = price_str if price_str else "0"
                volume = Decimal(raw_volume)
            except (InvalidOperation, TypeError):
                continue

            if volume == 0:
                removals.append(price)
            else:
                updates[price] = str(volume)

        if updates:
            pipeline.hset(redis_key, mapping=updates)

        if removals:
            pipeline.hdel(redis_key, *removals)

    async def _read_exchange_books(self, exchange_name, symbols):
        """Читает ASK/BID и meta по символам одним pipeline, чтобы снизить latency Redis."""
        pipeline = self.redis.pipeline()
        for symbol_name in symbols:
            pipeline.hgetall(self.side_key(exchange_name, symbol_name, "ASK"))
            pipeline.hgetall(self.side_key(exchange_name, symbol_name, "BID"))
            pipeline.hgetall(self.meta_key(exchange_name, symbol_name))

        response = await pipeline.execute()
        books = {}
        meta = {}
        for index, symbol_name in enumerate(symbols):
            asks = response[index * 3]
            bids = response[index * 3 + 1]
            meta_raw = response[index * 3 + 2]
            books[symbol_name] = {"ASK": asks, "BID": bids}
            meta[symbol_name] = self._decode_hash(meta_raw).get("updated_at")

        return books, meta

    def _is_fresh(self, updated_at_iso, max_age_seconds):
        if not updated_at_iso:
            return False
        try:
            updated_at = datetime.fromisoformat(updated_at_iso)
            now = datetime.now(timezone.utc)
            return (now - updated_at).total_seconds() <= max_age_seconds
        except Exception:
            return False

    async def read_raw_snapshot(self, exchanges, max_age_seconds=30):
        """Читает полный сырой снимок стаканов для всех запрошенных бирж."""
        result = {}

        for exchange_name in exchanges:
            result[exchange_name] = {}
            try:
                symbols = await self.redis.smembers(self.symbols_key(exchange_name))
            except Exception as error:
                self.logger.error("Read error while fetching symbols for %s: %s", exchange_name, error)
                continue

            symbol_names = sorted(
                symbol.decode() if isinstance(symbol, bytes) else symbol
                for symbol in symbols
            )

            try:
                books, meta = await self._read_exchange_books(exchange_name, symbol_names)
            except Exception as error:
                self.logger.error(
                    "Read error while fetching books for %s: %s",
                    exchange_name,
                    error,
                )
                continue

            for symbol_name, book in books.items():
                updated_at = meta.get(symbol_name)
                if not self._is_fresh(updated_at, max_age_seconds):
                    continue
                result[exchange_name][symbol_name] = {
                    "ASK": self._decode_levels(book["ASK"], "ASK"),
                    "BID": self._decode_levels(book["BID"], "BID"),
                    "_meta": {"updated_at": updated_at},
                }

        return result

    def _decode_levels(self, redis_hash, side):
        """Преобразует Redis hash в отсортированный список уровней цены/объёма."""
        levels = []
        for raw_price, raw_volume in redis_hash.items():
            try:
                price = Decimal(raw_price.decode() if isinstance(raw_price, bytes) else raw_price)
                volume = Decimal(raw_volume.decode() if isinstance(raw_volume, bytes) else raw_volume)
            except (InvalidOperation, TypeError):
                continue
            levels.append({"price": price, "volume": volume})

        levels.sort(key=lambda item: item["price"], reverse=side == "BID")
        return levels

    async def read_aggregated_snapshot(self, exchanges, target_value, max_levels, max_age_seconds=30):
        """Читает и агрегирует стаканы до целевой стоимости/лимита уровней."""
        result = {}

        for exchange_name in exchanges:
            result[exchange_name] = {}
            try:
                symbols = await self.redis.smembers(self.symbols_key(exchange_name))
            except Exception as error:
                self.logger.error(
                    "Read error while fetching symbols for aggregated snapshot %s: %s",
                    exchange_name,
                    error,
                )
                continue

            symbol_names = sorted(
                symbol.decode() if isinstance(symbol, bytes) else symbol
                for symbol in symbols
            )

            try:
                books, meta = await self._read_exchange_books(exchange_name, symbol_names)
            except Exception as error:
                self.logger.error(
                    "Read error while fetching aggregated books for %s: %s",
                    exchange_name,
                    error,
                )
                continue

            for symbol_name, book in books.items():
                if not self._is_fresh(meta.get(symbol_name), max_age_seconds):
                    continue
                asks = self.decode_book(book["ASK"])
                bids = self.decode_book(book["BID"])
                result[exchange_name][symbol_name] = {
                    "ASK": self.aggregate_side(asks, "ASK", target_value, max_levels),
                    "BID": self.aggregate_side(bids, "BID", target_value, max_levels),
                }

        return result

    def decode_book(self, redis_hash):
        """Декодирует Redis hash книги в словарь {price: volume} с Decimal."""
        book = {}

        for raw_price, raw_volume in redis_hash.items():
            try:
                price = Decimal(raw_price.decode() if isinstance(raw_price, bytes) else raw_price)
                volume = Decimal(raw_volume.decode() if isinstance(raw_volume, bytes) else raw_volume)
            except (InvalidOperation, TypeError):
                continue

            book[price] = volume

        return book

    def aggregate_side(self, book, side, target_value, max_levels):
        """Агрегирует сторону стакана и считает средневзвешенную цену исполнения."""
        if not book:
            return None

        sorted_levels = sorted(book.items(), key=lambda item: item[0], reverse=side == "BID")
        total_value = Decimal("0")
        total_volume = Decimal("0")
        levels_used = 0

        for price, volume in sorted_levels:
            total_value += price * volume
            total_volume += volume
            levels_used += 1

            if total_value >= target_value or levels_used >= max_levels:
                break

        if total_volume == 0 or total_value < target_value:
            return None

        return AggregatedSide(
            price=total_value / total_volume,
            volume=total_volume,
            value=total_value,
            levels=levels_used,
        )

    def _decode_hash(self, raw_hash):
        """Декодирует Redis hash {bytes: bytes} -> {str: str}."""
        return {
            (key.decode() if isinstance(key, bytes) else key): (
                value.decode() if isinstance(value, bytes) else value
            )
            for key, value in raw_hash.items()
        }

    async def read_arbitrage_event(self, event_key):
        """Читает состояние одного арбитражного события из Redis."""
        raw_state = await self.redis.hgetall(self.arbitrage_event_key(event_key))
        if not raw_state:
            return None

        decoded = self._decode_hash(raw_state)
        decoded["sent"] = decoded.get("sent", "0") == "1"
        try:
            decoded["data"] = json.loads(decoded.get("data", "{}"))
        except json.JSONDecodeError:
            self.logger.error("Corrupted JSON in arbitrage event %s", event_key)
            return None
        return decoded

    async def upsert_arbitrage_event(self, event_key, event_data, now_iso, existed=False):
        """Создает/обновляет запись арбитражного события с неизменным created_at."""
        redis_key = self.arbitrage_event_key(event_key)
        encoded_data = json.dumps(event_data, ensure_ascii=False)

        pipeline = self.redis.pipeline(transaction=True)
        pipeline.sadd(self.arbitrage_events_set_key(), event_key)
        pipeline.hsetnx(redis_key, "created_at", now_iso)
        pipeline.hsetnx(redis_key, "sent", "0")
        pipeline.hset(
            redis_key,
            mapping={
                "updated_at": now_iso,
                "data": encoded_data,
            },
        )

        results = await pipeline.execute()
        # hsetnx created_at возвращает 1 если поле создано, 0 если уже существовало
        created = results[1] if len(results) > 1 else 1
        return created == 0  # existed=True если created_at уже был

    async def mark_arbitrage_event_sent(self, event_key):
        """Отмечает событие как отправленное в Telegram."""
        await self.redis.hset(self.arbitrage_event_key(event_key), mapping={"sent": "1"})

    async def delete_arbitrage_event(self, event_key):
        """Удаляет событие из Redis (Hash + индексный Set)."""
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.delete(self.arbitrage_event_key(event_key))
        pipeline.srem(self.arbitrage_events_set_key(), event_key)
        await pipeline.execute()

    async def list_arbitrage_events(self):
        """Возвращает список всех активных событий из Redis."""
        raw_keys = await self.redis.smembers(self.arbitrage_events_set_key())
        event_keys = sorted(
            key.decode() if isinstance(key, bytes) else key
            for key in raw_keys
        )
        if not event_keys:
            return []

        pipeline = self.redis.pipeline()
        for event_key in event_keys:
            pipeline.hgetall(self.arbitrage_event_key(event_key))
        states = await pipeline.execute()

        events = []
        for event_key, raw_state in zip(event_keys, states):
            if not raw_state:
                # Запись могла исчезнуть между SMEMBERS и HGETALL; чистим индекс.
                await self.redis.srem(self.arbitrage_events_set_key(), event_key)
                continue
            decoded = self._decode_hash(raw_state)
            decoded["sent"] = decoded.get("sent", "0") == "1"
            try:
                decoded["data"] = json.loads(decoded.get("data", "{}"))
            except json.JSONDecodeError:
                self.logger.error("Corrupted JSON in arbitrage event %s, cleaning up index", event_key)
                await self.redis.srem(self.arbitrage_events_set_key(), event_key)
                continue
            events.append((event_key, decoded))

        return events
