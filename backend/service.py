import asyncio
import json
import time
import logging
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations

import redis.asyncio as redis
from redis.exceptions import RedisError

from backend.history_store import ArbitrageHistoryStore
from backend.spread_calculator import NetSpreadCalculator, NetSpreadResult
from common.redis_store import RedisOrderBookStore
from common.utils import normalize_symbol


@dataclass
class ArbitrageOpportunity:
    """Модель найденной арбитражной возможности между двумя биржами."""

    coin: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    spread_percent: Decimal
    net_spread: Decimal = Decimal("0")
    slippage_buy: Decimal = Decimal("0")
    slippage_sell: Decimal = Decimal("0")
    fee_total: Decimal = Decimal("0")
    confidence: Decimal = Decimal("0")
    data_age_ms: int = 0
    liquidity_exhausted: bool = False


class TelegramNotificationService:
    """Сервис отправки и редактирования Telegram-сообщений об арбитражных событиях."""

    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token or ""
        self.chat_id = chat_id or ""
        self.logger = logging.getLogger("telegram")
        self._min_send_interval = 1.0
        self._last_send_time = 0.0

    @property
    def enabled(self):
        """Возвращает True если настроены bot_token и chat_id."""
        return bool(self.bot_token and self.chat_id)

    async def send_new_event(self, event_data):
        """Отправляет новое сообщение о событии. Возвращает message_id или None."""
        if not self.enabled:
            return None
        text = self._build_start_message(event_data)
        return await self._send_message(text, parse_mode="Markdown")

    async def edit_closed_event(self, message_id, event_data, end_time_iso):
        """Редактирует отправленное сообщение обогащенными данными о завершении."""
        if not self.enabled or not message_id:
            return
        text = self._build_end_message(event_data, end_time_iso)
        await self._edit_message(message_id, text, parse_mode="Markdown")

    def _build_start_message(self, event_data):
        """Формирует Markdown-сообщение о старте арбитражного события."""
        coin_pair = self._format_coin_pair(event_data["coin"])
        buy_ex = event_data["buy_exchange"]
        sell_ex = event_data["sell_exchange"]
        spread = event_data.get("spread_percent", "0")
        net_spread = event_data.get("net_spread", "0")
        confidence = event_data.get("confidence", "0")
        buy_price = self._format_price(event_data.get("buy_price", "0"))
        sell_price = self._format_price(event_data.get("sell_price", "0"))
        fee_total = event_data.get("fee_total", "0")
        slippage_buy = event_data.get("slippage_buy", "0")
        slippage_sell = event_data.get("slippage_sell", "0")
        liq = event_data.get("liquidity_exhausted", "0") == "1"

        return (
            "🚀 *Арбитражный сигнал*\n"
            f"Coin: *{coin_pair}*\n"
            f"Direction: BUY `{buy_ex}` → SELL `{sell_ex}`\n"
            f"Buy price: `{buy_price}`\n"
            f"Sell price: `{sell_price}`\n"
            f"Gross spread: *{spread}%*\n"
            f"Net spread: `{net_spread}%`\n"
            f"Fee total: `{fee_total}%`\n"
            f"Slippage buy: `{slippage_buy}%`\n"
            f"Slippage sell: `{slippage_sell}%`\n"
            f"Confidence: `{confidence}%`\n"
            f"Liquidity exhausted: {'⚠️ ДА' if liq else '✅ Нет'}"
        )

    def _build_end_message(self, event_data, end_time_iso):
        """Формирует Markdown-сообщение о завершении события с обогащенной статистикой."""
        coin_pair = self._format_coin_pair(event_data["coin"])
        buy_ex = event_data["buy_exchange"]
        sell_ex = event_data["sell_exchange"]
        duration = self._calc_duration(event_data.get("start_time", ""), end_time_iso)
        max_spread = event_data.get("max_spread", "0")
        max_net = event_data.get("max_net_spread", "0")
        final_spread = event_data.get("spread_percent", "0")
        tick_count = event_data.get("tick_count", "N/A")
        max_conf = event_data.get("max_confidence", "0")
        min_conf = event_data.get("min_confidence", "0")

        try:
            marginality = float(max_spread) - float(final_spread)
            marginality_str = f"{marginality:.4f}"
        except (ValueError, TypeError):
            marginality_str = "N/A"

        return (
            "✅ *Арбитраж завершен*\n"
            f"Coin: *{coin_pair}*\n"
            f"Direction: BUY `{buy_ex}` → SELL `{sell_ex}`\n"
            f"\n📊 *Статистика:*\n"
            f"Duration: `{duration} sec`\n"
            f"Ticks survived: `{tick_count}`\n"
            f"Max gross spread: *{max_spread}%*\n"
            f"Max net spread: `{max_net}%`\n"
            f"Final spread: `{final_spread}%`\n"
            f"Marginality: `{marginality_str}%`\n"
            f"Confidence max: `{max_conf}%`\n"
            f"Confidence min: `{min_conf}%`"
        )

    async def _send_message(self, text, parse_mode=None):
        """Отправляет сообщение через Telegram API. Возвращает message_id."""
        now = time.monotonic()
        elapsed = now - self._last_send_time
        if elapsed < self._min_send_interval:
            await asyncio.sleep(self._min_send_interval - elapsed)

        payload_dict = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload_dict["parse_mode"] = parse_mode
        payload_dict["disable_web_page_preview"] = "true"
        payload = urllib.parse.urlencode(payload_dict).encode()
        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        try:
            response_body = await asyncio.to_thread(self._post, api_url, payload)
            self._last_send_time = time.monotonic()
            response_data = json.loads(response_body)
            return response_data.get("result", {}).get("message_id")
        except Exception as e:
            self.logger.error("Failed to send Telegram message: %s", e)
            return None

    async def _edit_message(self, message_id, text, parse_mode=None):
        """Редактирует существующее сообщение в Telegram."""
        payload_dict = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload_dict["parse_mode"] = parse_mode
        payload_dict["disable_web_page_preview"] = "true"
        payload = urllib.parse.urlencode(payload_dict).encode()
        api_url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"

        try:
            await asyncio.to_thread(self._post, api_url, payload)
            self.logger.info("Edited Telegram message %s", message_id)
        except Exception as e:
            self.logger.error("Failed to edit Telegram message %s: %s", message_id, e)

    def _post(self, url, payload):
        """Синхронный POST-запрос к Telegram API."""
        req = urllib.request.Request(url=url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                status_code = getattr(response, "status", None)
                body_text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as http_error:
            body_text = http_error.read().decode("utf-8", errors="replace")
            if http_error.code == 429:
                retry_after = 1
                try:
                    body_json = json.loads(body_text)
                    retry_after = body_json.get("parameters", {}).get("retry_after", 1)
                except Exception:
                    pass
                self.logger.error("Telegram rate limit 429, retry after %s", retry_after)
                raise RuntimeError(f"Telegram rate limit 429: retry_after={retry_after}") from http_error
            raise RuntimeError(f"Telegram HTTP error {http_error.code}: {body_text}") from http_error
        except urllib.error.URLError as url_error:
            raise RuntimeError(f"Telegram network error: {url_error}") from url_error

        if status_code != 200:
            raise RuntimeError(f"Telegram returned {status_code}: {body_text}")
        return body_text

    @staticmethod
    def _format_coin_pair(symbol):
        """Преобразует BTCUSDT → BTC/USDT."""
        normalized = symbol.upper()
        for quote in ["USDT", "USDC", "FDUSD", "TUSD", "BUSD", "BTC", "ETH", "BNB"]:
            if normalized.endswith(quote) and len(normalized) > len(quote):
                return f"{normalized[:-len(quote)]}/{quote}"
        return normalized

    @staticmethod
    def _format_price(value):
        """Форматирует цену."""
        try:
            d = Decimal(str(value))
            return f"{d:.6f}".rstrip("0").rstrip(".") or "0"
        except Exception:
            return str(value)

    @staticmethod
    def _calc_duration(start_iso, end_iso):
        """Считает длительность в секундах."""
        try:
            start = datetime.fromisoformat(start_iso)
            end = datetime.fromisoformat(end_iso)
            return max(0, int((end - start).total_seconds()))
        except Exception:
            return 0

    def send_startup_message(self, text):
        """Отправляет сервисное сообщение (синхронная обертка для startup)."""
        if not self.enabled:
            return
        payload_dict = {"chat_id": self.chat_id, "text": text}
        payload = urllib.parse.urlencode(payload_dict).encode()
        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            self._post(api_url, payload)
        except Exception as e:
            self.logger.error("Startup message failed: %s", e)


class BackendService:
    def __init__(self, store, settings, history_store, runtime_settings_store=None):
        self.store = store
        self.settings = settings
        self.history_store = history_store
        self.runtime_settings_store = runtime_settings_store
        self.telegram = TelegramNotificationService(
            bot_token=self.settings["telegram"]["bot_token"],
            chat_id=self.settings["telegram"]["chat_id"],
        )
        self.spread_calculator = NetSpreadCalculator(
            fee_config=self.settings.get("fees", {}),
            withdrawal_fee_usdt=self.settings["backend"].get("withdrawal_fee_usdt", Decimal("0")),
        )
        self._runtime_settings = {}
        self._tier_threshold_cache = self._build_tier_threshold_cache()
        self.last_raw_payload = {
            "type": "raw",
            "timestamp": self.now_iso(),
            "data": {},
        }
        self.last_arbitrage_payload = {
            "type": "arbitrage",
            "timestamp": self.now_iso(),
            "data": [],
        }
        self.task = None
        self.backend_logger = logging.getLogger("backend")
        self.arbitrage_logger = logging.getLogger("arbitrage")

    async def _load_runtime_settings(self):
        """Загружает runtime-настройки из Redis/файла и применяет их."""
        if self.runtime_settings_store is not None:
            try:
                new_settings = await self.runtime_settings_store.load()
                settings_changed = new_settings != self._runtime_settings
                self._runtime_settings = new_settings
                if settings_changed:
                    self._rebuild_fee_config_from_runtime()
                    self._tier_threshold_cache = self._build_tier_threshold_cache()
            except Exception as e:
                self.backend_logger.error("Failed to load runtime settings: %s", e)
        else:
            self._rebuild_fee_config_from_runtime()
            self._tier_threshold_cache = self._build_tier_threshold_cache()

    def _runtime_value(self, key, default=None):
        """Возвращает значение runtime-настройки или default."""
        return self._runtime_settings.get(key, default)

    def _rebuild_fee_config_from_runtime(self):
        """Перестраивает fee_config из runtime-настроек."""
        for exchange in self.settings.get("exchanges", []):
            runtime_key = f"fee_{exchange}_taker"
            runtime_fee = self._runtime_settings.get(runtime_key)
            if runtime_fee is not None:
                if exchange not in self.spread_calculator.fee_config:
                    self.spread_calculator.fee_config[exchange] = {}
                self.spread_calculator.fee_config[exchange]["taker"] = Decimal(str(runtime_fee))
        # Withdrawal fee
        runtime_wd = self._runtime_settings.get("withdrawal_fee_usdt")
        if runtime_wd is not None:
            self.spread_calculator.withdrawal_fee_usdt = Decimal(str(runtime_wd))

    async def run(self):
        """Основной цикл: читает стаканы, ищет арбитраж, управляет событиями и обновляет HTTP payload."""
        await self.notify_backend_started()
        self.backend_logger.info("Backend main loop started.")
        while True:
            try:
                await self._load_runtime_settings()

                # --- ФАЗА 1: Чтение + быстрое обновление payload ---
                raw_snapshot = await self.store.read_raw_snapshot(self.settings["exchanges"])

                # Быстрое обновление raw payload (фронт сразу видит свежие данные)
                self.last_raw_payload = {
                    "type": "raw",
                    "timestamp": self.now_iso(),
                    "data": self.normalize_payload(raw_snapshot),
                }

                # Агрегируем в памяти из raw (НЕ читаем из Redis второй раз)
                aggregated_snapshot = self._aggregate_in_memory(
                    raw_snapshot,
                    target_value=self.settings["backend"]["target_value"],
                    max_levels=self.settings["backend"]["max_levels"],
                )

                # --- ФАЗА 2: Поиск арбитража ---
                opportunities = self.find_arbitrage(aggregated_snapshot, raw_snapshot)
                await self.sync_arbitrage_events(opportunities)

                # Передаем ключи текущих возможностей для cumulative time tracking
                current_opportunity_keys = {self.opportunity_key(item) for item in opportunities}
                try:
                    await self.process_arbitrage_events(current_opportunity_keys)
                except Exception as e:
                    self.arbitrage_logger.error("Arbitrage events processing failed: %s", e)

                # --- ФАЗА 3: Финальное обновление arbitrage payload ---
                active_payload = await self.build_active_payload_from_redis()
                self.last_arbitrage_payload = {
                    "type": "arbitrage",
                    "timestamp": self.now_iso(),
                    "data": active_payload,
                }

                self.backend_logger.debug("Backend loop iteration completed.")
            except Exception as e:
                self.backend_logger.error("Backend loop failed: %s", e)
                # При ошибке — payload уже обновлен в фазе 1

            render_interval = float(
                self._runtime_value(
                    "render_interval_seconds",
                    self.settings["backend"]["render_interval_seconds"],
                )
            )
            await asyncio.sleep(render_interval)

    def _aggregate_in_memory(self, raw_snapshot, target_value, max_levels):
        """Агрегирует raw стаканы в памяти вместо повторного чтения из Redis."""
        result = {}
        for exchange_name, symbols in raw_snapshot.items():
            result[exchange_name] = {}
            for symbol_name, sides in symbols.items():
                ask_agg = self.store.aggregate_side(
                    {Decimal(level["price"]): Decimal(level["volume"]) for level in sides.get("ASK", [])},
                    "ASK", target_value, max_levels,
                )
                bid_agg = self.store.aggregate_side(
                    {Decimal(level["price"]): Decimal(level["volume"]) for level in sides.get("BID", [])},
                    "BID", target_value, max_levels,
                )
                if ask_agg or bid_agg:
                    result[exchange_name][symbol_name] = {
                        "ASK": ask_agg,
                        "BID": bid_agg,
                    }
        return result

    async def start(self):
        """Запускает backend-цикл как фоновую задачу."""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
            self.backend_logger.info("Backend background task started.")

    async def stop(self):
        """Останавливает фоновую задачу backend-цикла."""
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.backend_logger.info("Backend background task stopped.")

    def get_raw_payload(self):
        return self.last_raw_payload.copy()

    def get_arbitrage_payload(self):
        return self.last_arbitrage_payload.copy()

    async def sync_arbitrage_events(self, opportunities):
        """Синхронизирует текущие возможности с Redis-хранилищем событий."""
        now_iso = self.now_iso()
        for item in opportunities:
            event_key = self.opportunity_key(item)
            current_state = await self.store.read_arbitrage_event(event_key)
            max_spread = item.spread_percent
            max_net_spread = item.net_spread
            if current_state is not None:
                current_max = Decimal(current_state["data"].get("max_spread", "0"))
                max_spread = max(max_spread, current_max)
                current_max_net = Decimal(current_state["data"].get("max_net_spread", "0"))
                max_net_spread = max(max_net_spread, current_max_net)

            event_data = {
                "coin": item.coin,
                "buy_exchange": item.buy_exchange,
                "sell_exchange": item.sell_exchange,
                "buy_price": str(item.buy_price),
                "sell_price": str(item.sell_price),
                "spread_percent": str(item.spread_percent),
                "net_spread": str(item.net_spread),
                "slippage_buy": str(item.slippage_buy),
                "slippage_sell": str(item.slippage_sell),
                "fee_total": str(item.fee_total),
                "confidence": str(item.confidence),
                "data_age_ms": str(item.data_age_ms),
                "liquidity_exhausted": "1" if item.liquidity_exhausted else "0",
                "start_time": current_state["created_at"] if current_state else now_iso,
                "max_spread": str(max_spread),
                "max_net_spread": str(max_net_spread),
                "tick_count": str(int(current_state["data"].get("tick_count", "0")) + 1) if current_state else "1",
                "max_confidence": str(max(
                    Decimal(str(item.confidence)),
                    Decimal(current_state["data"].get("max_confidence", "0")) if current_state else Decimal("0")
                )),
                "min_confidence": str(min(
                    Decimal(str(item.confidence)),
                    Decimal(current_state["data"].get("min_confidence", "100")) if current_state else Decimal("100")
                )),
            }
            existed = await self.store.upsert_arbitrage_event(
                event_key=event_key,
                event_data=event_data,
                now_iso=now_iso,
            )
            if existed:
                self.arbitrage_logger.info(
                    "Arbitrage event updated %s BUY %s SELL %s",
                    item.coin,
                    item.buy_exchange,
                    item.sell_exchange,
                )
            else:
                self.arbitrage_logger.info(
                    "Arbitrage event created %s BUY %s SELL %s spread=%s%% net=%s%% conf=%s%%",
                    item.coin,
                    item.buy_exchange,
                    item.sell_exchange,
                    item.spread_percent,
                    item.net_spread,
                    item.confidence,
                )

    async def build_active_payload_from_redis(self):
        """Собирает payload активных событий напрямую из Redis."""
        events = await self.store.list_arbitrage_events()
        payload = []
        for _, event_state in events:
            try:
                Decimal(event_state["data"]["spread_percent"])
            except Exception:
                self.arbitrage_logger.warning(
                    "Skipping event %s due to invalid spread_percent", event_state
                )
                continue
            payload.append(event_state["data"])
        payload.sort(key=lambda item: Decimal(item["spread_percent"]), reverse=True)
        return payload

    async def archive_event(self, state, end_time_iso):
        """Пишет завершенное событие в persistent history (SQLite)."""
        await asyncio.to_thread(
            self.history_store.insert_record,
            state["coin"],
            state["buy_exchange"],
            state["sell_exchange"],
            state["start_time"],
            end_time_iso,
            state["max_spread"],
            net_spread=state.get("max_net_spread", "0"),
            fee_total=state.get("fee_total", "0"),
            slippage_buy=state.get("slippage_buy", "0"),
            slippage_sell=state.get("slippage_sell", "0"),
            confidence=state.get("confidence", "0"),
            data_age_ms=int(state.get("data_age_ms", "0") or 0),
        )

    async def process_arbitrage_events(self, current_opportunity_keys):
        """Обрабатывает события: cumulative time tracking, delayed send, expire."""
        events = await self.store.list_arbitrage_events()
        now = datetime.now(timezone.utc)
        expire_seconds = float(self._runtime_value(
            "event_expire_seconds",
            self.settings["backend"]["event_expire_seconds"]
        ))
        confidence_min = Decimal(str(self._runtime_value(
            "confidence_min",
            self.settings["backend"].get("confidence_min", "70")
        )))

        for event_key, state in events:
            try:
                data = state["data"]
                cumulative = float(state.get("cumulative_active_seconds", "0"))
                last_active_str = state.get("last_active_at", state["created_at"])
                last_active = datetime.fromisoformat(last_active_str)
                is_active_now = event_key in current_opportunity_keys
                time_since_active = (now - last_active).total_seconds()

                if is_active_now:
                    # --- Событие активно в этом тике ---
                    delta = (now - last_active).total_seconds()
                    if delta > 0:
                        cumulative += delta
                        await self.store.accumulate_active_time(
                            event_key, cumulative, now.isoformat()
                        )

                    # Накопили 15 секунд и еще не отправляли?
                    if cumulative >= 15.0 and not state["sent"]:
                        event_confidence = Decimal(str(data.get("confidence", "0")))
                        if event_confidence >= confidence_min:
                            msg_id = await self.telegram.send_new_event(data)
                            if msg_id:
                                await self.store.mark_arbitrage_event_sent(event_key, msg_id)
                                self.arbitrage_logger.info(
                                    "Event sent to Telegram %s BUY %s SELL %s conf=%s%% msg_id=%s",
                                    data["coin"], data["buy_exchange"],
                                    data["sell_exchange"], event_confidence, msg_id,
                                )
                                # Публикуем торговый сигнал
                                await self._publish_trade_signal(data, "OPEN")
                        else:
                            self.arbitrage_logger.info(
                                "Event below confidence threshold %s BUY %s SELL %s conf=%s%% (need %s%%)",
                                data["coin"], data["buy_exchange"],
                                data["sell_exchange"], event_confidence, confidence_min,
                            )

                else:
                    # --- Событие НЕ активно ---
                    if time_since_active >= 3.0:
                        if not state["sent"]:
                            # Не отправляли и пропало >=3сек -> удаляем
                            await self.store.delete_arbitrage_event(event_key)
                            self.arbitrage_logger.info(
                                "Event deleted (gap >= 3s, not sent) %s", event_key
                            )
                        elif time_since_active >= expire_seconds:
                            # Уже отправили и пропало >=expire -> закрываем
                            end_time_iso = now.isoformat()
                            msg_id = state.get("telegram_message_id")
                            if msg_id:
                                try:
                                    await self.telegram.edit_closed_event(
                                        int(msg_id), data, end_time_iso
                                    )
                                except Exception as e:
                                    self.arbitrage_logger.error(
                                        "Failed to edit Telegram message %s: %s", msg_id, e
                                    )
                            # Публикуем сигнал закрытия (не блокируем cleanup)
                            try:
                                await self._publish_trade_signal(data, "CLOSE")
                            except Exception as e:
                                self.arbitrage_logger.error(
                                    "Failed to publish close signal for %s: %s", event_key, e
                                )
                            # Архивируем (не блокируем cleanup при ошибке SQLite)
                            try:
                                await self.archive_event(data, end_time_iso)
                            except Exception as e:
                                self.arbitrage_logger.error(
                                    "Failed to archive event %s: %s", event_key, e
                                )
                            # Удаляем из Redis в любом случае
                            await self.store.delete_arbitrage_event(event_key)
                            self.arbitrage_logger.info(
                                "Event closed %s BUY %s SELL %s duration=%ss",
                                data["coin"], data["buy_exchange"],
                                data["sell_exchange"],
                                int(cumulative),
                            )

            except Exception as error:
                self.arbitrage_logger.error(
                    "Event processing failed for %s: %s", event_key, error
                )

    async def _publish_trade_signal(self, event_data, signal_type):
        """Публикует торговый сигнал в Redis Pub/Sub для торгового бота."""
        try:
            signal = {
                "type": "trade_signal",
                "signal_type": signal_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_key": f"{event_data['coin']}:{event_data['buy_exchange']}:{event_data['sell_exchange']}",
                "coin": event_data["coin"],
                "buy_exchange": event_data["buy_exchange"],
                "sell_exchange": event_data["sell_exchange"],
                "buy_price": event_data.get("buy_price", "0"),
                "sell_price": event_data.get("sell_price", "0"),
                "spread_percent": event_data.get("spread_percent", "0"),
                "net_spread": event_data.get("net_spread", "0"),
                "confidence": event_data.get("confidence", "0"),
                "fee_total": event_data.get("fee_total", "0"),
                "target_value_usdt": str(self.settings["backend"]["target_value"]),
            }
            await self.store.redis.publish("trade:signals", json.dumps(signal, ensure_ascii=False))
            self.arbitrage_logger.info(
                "Trade signal published: %s %s", signal_type, signal["event_key"]
            )
        except Exception as e:
            self.arbitrage_logger.error("Failed to publish trade signal: %s", e)

    def split_symbol(self, symbol):
        """Разбивает символ вида BASEQUOTE на BASE и QUOTE."""
        normalized = self.normalize_symbol(symbol)
        known_quotes = [
            "USDT",
            "USDC",
            "FDUSD",
            "TUSD",
            "BUSD",
            "BTC",
            "ETH",
            "BNB",
            "EUR",
            "TRY",
            "RUB",
        ]
        for quote in known_quotes:
            if normalized.endswith(quote) and len(normalized) > len(quote):
                return normalized[: -len(quote)], quote
        return normalized[:-4], normalized[-4:]

    def opportunity_key(self, opportunity):
        """Возвращает короткий ключ возможности: coin+buy_exchange+sell_exchange."""
        return f"{opportunity.coin}:{opportunity.buy_exchange}:{opportunity.sell_exchange}"

    def now_iso(self):
        """Возвращает текущее UTC-время в ISO-формате."""
        return datetime.now(timezone.utc).isoformat()

    def normalize_payload(self, value):
        """Рекурсивно приводит payload к JSON-сериализуемому виду."""
        if isinstance(value, Decimal):
            s = str(value)
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s if s else "0"
        if isinstance(value, dict):
            return {key: self.normalize_payload(inner) for key, inner in value.items()}
        if isinstance(value, list):
            return [self.normalize_payload(item) for item in value]
        if hasattr(value, "price") and hasattr(value, "volume") and hasattr(value, "value"):
            return {
                "price": self.normalize_payload(value.price),
                "volume": self.normalize_payload(value.volume),
                "value": self.normalize_payload(value.value),
                "levels": value.levels,
            }
        return value

    async def notify_backend_started(self):
        """Шлет сервисное уведомление о запуске backend."""
        startup_message = "[BACKEND] Бэкенд поднят и готов к обработке арбитража."
        try:
            self.telegram.send_startup_message(startup_message)
            self.backend_logger.info("Backend startup notification sent to Telegram.")
        except Exception as e:
            self.telegram.logger.error("Startup notification error: %s", e)

    def normalize_symbol(self, symbol):
        """Нормализует символ: uppercase + только буквы/цифры."""
        return normalize_symbol(symbol)

    def _build_tier_threshold_cache(self):
        """Строит кэш {normalized_symbol -> threshold Decimal}."""
        cache = {}
        tiers = self.settings.get("tiers", {})
        for tier_num, tier_data in tiers.items():
            # Runtime override имеет приоритет
            runtime_threshold = self._runtime_settings.get(f"tier_{tier_num}_threshold")
            threshold = Decimal(str(runtime_threshold)) if runtime_threshold is not None else tier_data.get("threshold", Decimal("0.3"))
            for symbol in tier_data.get("pairs", []):
                normalized = self.normalize_symbol(symbol)
                cache[normalized] = Decimal(str(threshold))
        return cache

    def get_threshold_for_symbol(self, symbol):
        """Возвращает порог спреда для символа на основе Tier-классификации."""
        normalized = self.normalize_symbol(symbol)
        return self._tier_threshold_cache.get(
            normalized, self.settings["backend"]["arbitrage_min_spread_percent"]
        )

    def calculate_confidence(self, data_age_ms, liquidity_exhausted, target_value, actual_quote_value):
        """
        Расчет confidence score (0–100).
        Факторы:
        - Возраст данных (40%)
        - Недостаток ликвидности (30%)
        - Глубина стакана (30%)
        """
        score = Decimal("0")

        # Возраст данных
        if data_age_ms < 500:
            score += Decimal("40")
        elif data_age_ms < 2000:
            score += Decimal("20")
        else:
            score += Decimal("0")

        # Ликвидность
        if not liquidity_exhausted:
            score += Decimal("30")

        # Глубина
        if actual_quote_value > 0:
            depth_ratio = min(Decimal("1"), target_value / actual_quote_value)
            score += depth_ratio * Decimal("30")

        return score.quantize(Decimal("0.1"))

    def extract_symbol_views(self, snapshot):
        """Группирует данные по нормализованному символу независимо от формата биржи."""
        by_symbol = {}
        for exchange_name, symbols in snapshot.items():
            for raw_symbol, sides in symbols.items():
                normalized = self.normalize_symbol(raw_symbol)
                by_symbol.setdefault(normalized, []).append(
                    {
                        "exchange": exchange_name,
                        "ask": sides.get("ASK"),
                        "bid": sides.get("BID"),
                    }
                )
        return by_symbol

    def valid_side(self, side):
        """Проверяет валидность агрегированной стороны стакана для расчетов."""
        if side is None:
            return False
        if side.volume is None or side.price is None:
            return False
        if side.volume <= 0 or side.price <= 0:
            return False
        return True

    def calculate_spread_percent(self, buy_price, sell_price):
        """Считает процент спреда между ценой покупки и продажи."""
        return ((sell_price - buy_price) / buy_price) * Decimal("100")

    def find_arbitrage(self, aggregated_snapshot, raw_snapshot):
        """Ищет арбитражные возможности по всем парам бирж для каждого символа."""
        opportunities = []
        by_symbol = self.extract_symbol_views(aggregated_snapshot)
        target_value = self.settings["backend"]["target_value"]

        for symbol, entries in by_symbol.items():
            threshold = self.get_threshold_for_symbol(symbol)
            for entry_a, entry_b in combinations(entries, 2):
                self.maybe_append_opportunity(
                    opportunities, symbol, entry_a, entry_b, threshold,
                    raw_snapshot, target_value
                )
                self.maybe_append_opportunity(
                    opportunities, symbol, entry_b, entry_a, threshold,
                    raw_snapshot, target_value
                )

        opportunities.sort(key=lambda item: item.spread_percent, reverse=True)
        return opportunities

    def maybe_append_opportunity(
        self, opportunities, symbol, buy_entry, sell_entry, threshold,
        raw_snapshot, target_value
    ):
        """Добавляет возможность в список, если она проходит проверки и порог."""
        if buy_entry["exchange"] == sell_entry["exchange"]:
            return

        ask = buy_entry["ask"]
        bid = sell_entry["bid"]
        if not self.valid_side(ask) or not self.valid_side(bid):
            return

        try:
            spread_percent = self.calculate_spread_percent(ask.price, bid.price)
        except (InvalidOperation, ZeroDivisionError):
            return

        if spread_percent < threshold:
            return

        buy_exchange = buy_entry["exchange"]
        sell_exchange = sell_entry["exchange"]

        # Получаем сырые уровни стакана для расчета net spread
        raw_buy = raw_snapshot.get(buy_exchange, {}).get(symbol, {})
        raw_sell = raw_snapshot.get(sell_exchange, {}).get(symbol, {})
        buy_asks = raw_buy.get("ASK", [])
        sell_bids = raw_sell.get("BID", [])

        net_result = self.spread_calculator.calculate_net_spread(
            gross_spread_pct=spread_percent,
            buy_exchange=buy_exchange,
            sell_exchange=sell_exchange,
            orderbook_buy_asks=buy_asks,
            orderbook_sell_bids=sell_bids,
            target_quote_value=target_value,
        )

        # Возраст данных
        buy_meta = raw_buy.get("_meta", {})
        sell_meta = raw_sell.get("_meta", {})
        data_age_ms = self._calculate_data_age_ms(buy_meta, sell_meta)

        # Confidence score
        actual_liquidity = min(
            ask.value if ask else Decimal("0"),
            bid.value if bid else Decimal("0"),
        )
        confidence = self.calculate_confidence(
            data_age_ms=data_age_ms,
            liquidity_exhausted=net_result.liquidity_exhausted,
            target_value=target_value,
            actual_quote_value=actual_liquidity,
        )

        opportunities.append(
            ArbitrageOpportunity(
                coin=symbol,
                buy_exchange=buy_exchange,
                sell_exchange=sell_exchange,
                buy_price=ask.price,
                sell_price=bid.price,
                spread_percent=spread_percent.quantize(Decimal("0.0001")),
                net_spread=net_result.net_spread,
                slippage_buy=net_result.slippage_buy,
                slippage_sell=net_result.slippage_sell,
                fee_total=net_result.fee_total,
                confidence=confidence,
                data_age_ms=data_age_ms,
                liquidity_exhausted=net_result.liquidity_exhausted,
            )
        )

    def _calculate_data_age_ms(self, buy_meta, sell_meta):
        """Вычисляет максимальный возраст данных в миллисекундах."""
        now = datetime.now(timezone.utc)
        ages = []
        for meta in (buy_meta, sell_meta):
            ts = meta.get("updated_at")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age = (now - dt).total_seconds()
                    ages.append(int(age * 1000))
                except Exception:
                    pass
        return max(ages) if ages else 0
