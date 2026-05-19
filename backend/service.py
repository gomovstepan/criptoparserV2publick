import asyncio
import json
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
from urllib import error, parse, request

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


class TelegramNotifier:
    def __init__(self, bot_token, chat_id, dedup_ttl_seconds=60.0):
        self.bot_token = bot_token or ""
        self.chat_id = chat_id or ""
        # dedup_ttl_seconds — окно дедупликации, чтобы не спамить одинаковыми сигналами.
        self.dedup_ttl_seconds = dedup_ttl_seconds
        # last_sent_at хранит момент отправки сигнала по ключу возможности.
        self.last_sent_at = {}
        self.logger = logging.getLogger("telegram")
        # Минимальный интервал между отправками в Telegram (сек) — защита от 429.
        self._min_send_interval = 1.0
        self._last_send_time = 0.0

    @property
    def enabled(self):
        return bool(self.bot_token and self.chat_id)

    def should_send(self, opportunity):
        key = self._opportunity_key(opportunity)
        now = time.monotonic()
        last_ts = self.last_sent_at.get(key)
        if last_ts is not None and (now - last_ts) < self.dedup_ttl_seconds:
            return False
        return True

    def mark_sent(self, opportunity):
        key = self._opportunity_key(opportunity)
        self.last_sent_at[key] = time.monotonic()

    def cleanup_cache(self):
        now = time.monotonic()
        expired_keys = [
            key
            for key, last_ts in self.last_sent_at.items()
            if (now - last_ts) > self.dedup_ttl_seconds
        ]
        for key in expired_keys:
            del self.last_sent_at[key]

    def _opportunity_key(self, opportunity):
        return f"{opportunity.coin}:{opportunity.buy_exchange}:{opportunity.sell_exchange}"

    async def send(self, opportunity):
        self.cleanup_cache()
        if not self.enabled or not self.should_send(opportunity):
            return

        text = (
            f"Coin: {opportunity.coin}\n"
            f"Direction: BUY {opportunity.buy_exchange} -> SELL {opportunity.sell_exchange}\n"
            f"Buy price: {opportunity.buy_price}\n"
            f"Sell price: {opportunity.sell_price}\n"
            f"Spread: {opportunity.spread_percent}%"
        )
        await self.send_text(text)
        self.mark_sent(opportunity)
        self.logger.info(
            "Message sent successfully for %s BUY %s SELL %s spread=%s%%",
            opportunity.coin,
            opportunity.buy_exchange,
            opportunity.sell_exchange,
            opportunity.spread_percent,
        )

    async def send_text(self, text, parse_mode=None, disable_web_page_preview=False):
        if not self.enabled:
            return

        # Rate limiting: не чаще одного сообщения в _min_send_interval секунд.
        now = time.monotonic()
        elapsed = now - self._last_send_time
        if elapsed < self._min_send_interval:
            await asyncio.sleep(self._min_send_interval - elapsed)
        self._last_send_time = time.monotonic()

        payload_dict = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload_dict["parse_mode"] = parse_mode
        if disable_web_page_preview:
            payload_dict["disable_web_page_preview"] = "true"
        payload = parse.urlencode(payload_dict).encode()
        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        try:
            await asyncio.to_thread(self._post, api_url, payload)
        except RuntimeError as exc:
            if "Telegram rate limit 429" in str(exc):
                retry_after = 1
                try:
                    retry_after = int(str(exc).split("retry_after=")[1])
                except Exception:
                    pass
                self.logger.warning(
                    "Telegram rate limit hit, sleeping %s seconds", retry_after
                )
                await asyncio.sleep(retry_after)
                return
            raise

    def _post(self, url, payload):
        req = request.Request(url=url, data=payload, method="POST")
        try:
            with request.urlopen(req, timeout=10) as response:
                status_code = getattr(response, "status", None)
                body_text = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as http_error:
            body_text = http_error.read().decode("utf-8", errors="replace")
            if http_error.code == 429:
                retry_after = 1
                try:
                    body_json = json.loads(body_text)
                    retry_after = body_json.get("parameters", {}).get("retry_after", 1)
                except Exception:
                    pass
                self.logger.error(
                    "Telegram API HTTP error 429: retry after %s: %s", retry_after, body_text
                )
                raise RuntimeError(
                    f"Telegram rate limit 429: retry_after={retry_after}"
                ) from http_error
            self.logger.error("Telegram API HTTP error %s: %s", http_error.code, body_text)
            raise RuntimeError(
                f"Telegram HTTP error {http_error.code}: {body_text}"
            ) from http_error
        except error.URLError as url_error:
            self.logger.error("Telegram network error: %s", url_error)
            raise RuntimeError(f"Telegram network error: {url_error}") from url_error

        if status_code != 200:
            self.logger.error("Telegram non-200 response %s: %s", status_code, body_text)
            raise RuntimeError(f"Telegram вернул код {status_code}: {body_text}")

        try:
            response_payload = json.loads(body_text)
        except json.JSONDecodeError as decode_error:
            self.logger.error("Telegram returned non-JSON response: %s", body_text)
            raise RuntimeError(f"Telegram вернул не-JSON ответ: {body_text}") from decode_error

        if not isinstance(response_payload, dict) or response_payload.get("ok") is not True:
            self.logger.error("Telegram API error payload: %s", response_payload)
            raise RuntimeError(f"Telegram API ошибка: {response_payload}")


class BackendService:
    def __init__(self, store, settings, history_store, runtime_settings_store=None):
        self.store = store
        self.settings = settings
        self.history_store = history_store
        self.runtime_settings_store = runtime_settings_store
        self.telegram = TelegramNotifier(
            bot_token=self.settings["telegram"]["bot_token"],
            chat_id=self.settings["telegram"]["chat_id"],
            dedup_ttl_seconds=self.settings["backend"].get("telegram_dedup_ttl_seconds", 60.0),
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

                try:
                    await self.process_arbitrage_events()
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
                # При ошибке — payload уже обновлён в фазе 1

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

    async def process_arbitrage_events(self):
        """Обрабатывает события: delayed send и expire + архивирование."""
        events = await self.store.list_arbitrage_events()
        now = datetime.now(timezone.utc)
        send_delay = float(self._runtime_value(
            "event_send_delay_seconds",
            self.settings["backend"]["event_send_delay_seconds"]
        ))
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
                created_at = datetime.fromisoformat(state["created_at"])
                updated_at = datetime.fromisoformat(state["updated_at"])

                if (now - created_at).total_seconds() > send_delay and not state["sent"]:
                    event_confidence = Decimal(str(data.get("confidence", "0")))
                    if event_confidence >= confidence_min:
                        await self.send_new_event_message(data)
                        await self.store.mark_arbitrage_event_sent(event_key)
                        self.arbitrage_logger.info(
                            "Arbitrage event sent to Telegram %s BUY %s SELL %s conf=%s%%",
                            data["coin"],
                            data["buy_exchange"],
                            data["sell_exchange"],
                            event_confidence,
                        )
                    else:
                        self.arbitrage_logger.info(
                            "Arbitrage event skipped Telegram (low confidence) %s BUY %s SELL %s conf=%s%%",
                            data["coin"],
                            data["buy_exchange"],
                            data["sell_exchange"],
                            event_confidence,
                        )
                        # НЕ помечаем sent=True — попробуем отправить позже если confidence вырастет

                if (now - updated_at).total_seconds() > expire_seconds:
                    end_time_iso = now.isoformat()
                    try:
                        await self.send_closed_event_message(data, end_time_iso)
                    except Exception as e:
                        self.arbitrage_logger.error(
                            "Failed to send closed event message for %s: %s", event_key, e
                        )
                    try:
                        await self.archive_event(data, end_time_iso)
                    except Exception as e:
                        self.arbitrage_logger.error(
                            "Failed to archive event %s: %s", event_key, e
                        )
                    try:
                        await self.store.delete_arbitrage_event(event_key)
                        self.arbitrage_logger.info(
                            "Arbitrage event closed %s BUY %s SELL %s",
                            data["coin"],
                            data["buy_exchange"],
                            data["sell_exchange"],
                        )
                    except Exception as e:
                        self.arbitrage_logger.error(
                            "Failed to delete arbitrage event %s from Redis: %s", event_key, e
                        )
            except Exception as error:
                self.arbitrage_logger.error(
                    "Arbitrage event processing failed for key %s: %s",
                    event_key,
                    error,
                )

    async def send_new_event_message(self, event_data):
        """Отправляет в Telegram сообщение о новом событии."""
        text = self.build_start_event_message(event_data)
        await self.telegram.send_text(
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    async def send_closed_event_message(self, event_data, end_time_iso):
        """Отправляет в Telegram сообщение о завершении события."""
        text = self.build_end_event_message(event_data, end_time_iso)
        await self.telegram.send_text(
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    def build_start_event_message(self, event_data):
        """Формирует Telegram-сообщение о старте арбитражного события."""
        coin_pair = self.format_coin_pair(event_data["coin"])
        buy_link = self.format_exchange_link(event_data["buy_exchange"], event_data["coin"])
        sell_link = self.format_exchange_link(event_data["sell_exchange"], event_data["coin"])
        spread_value = self.format_percent(event_data["spread_percent"])
        net_spread = self.format_percent(event_data.get("net_spread", "0"))
        confidence = self.format_percent(event_data.get("confidence", "0"))
        buy_price = self.format_price(event_data["buy_price"])
        sell_price = self.format_price(event_data["sell_price"])
        return (
            "🚀 *Арбитражный сигнал*\n"
            f"Coin: *{coin_pair}*\n"
            f"Direction: BUY {buy_link} → SELL {sell_link}\n"
            f"Buy price: `{buy_price}`\n"
            f"Sell price: `{sell_price}`\n"
            f"Gross spread: *{spread_value}%*\n"
            f"Net spread: `{net_spread}%`\n"
            f"Confidence: `{confidence}%`"
        )

    def build_end_event_message(self, event_data, end_time_iso):
        """Формирует Telegram-сообщение о завершении арбитражного события."""
        coin_pair = self.format_coin_pair(event_data["coin"])
        buy_link = self.format_exchange_link(event_data["buy_exchange"], event_data["coin"])
        sell_link = self.format_exchange_link(event_data["sell_exchange"], event_data["coin"])
        started_at = self.format_timestamp(event_data["start_time"])
        ended_at = self.format_timestamp(end_time_iso)
        duration_seconds = self.calculate_duration_seconds(event_data["start_time"], end_time_iso)
        max_spread = self.format_percent(event_data["max_spread"])
        max_net_spread = self.format_percent(event_data.get("max_net_spread", "0"))
        return (
            "✅ *Арбитраж завершен*\n"
            f"Coin: *{coin_pair}*\n"
            f"Direction: BUY {buy_link} → SELL {sell_link}\n"
            f"Started at: `{started_at}`\n"
            f"Ended at: `{ended_at}`\n"
            f"Duration: `{duration_seconds} sec`\n"
            f"Max gross spread: *{max_spread}%*\n"
            f"Max net spread: `{max_net_spread}%`"
        )

    def format_exchange_link(self, exchange_name, symbol):
        """Возвращает Markdown-ссылку на торговую страницу биржи."""
        url = self.generate_exchange_url(exchange_name, symbol)
        if not url:
            return exchange_name
        return f"[{exchange_name}]({url})"

    def generate_exchange_url(self, exchange_name, symbol):
        """Генерирует URL торговой страницы биржи для конкретного инструмента."""
        base, quote = self.split_symbol(symbol)
        exchange = exchange_name.lower()
        if exchange == "binance":
            return f"https://www.binance.com/en/trade/{base}_{quote}"
        if exchange == "kucoin":
            return f"https://www.kucoin.com/trade/{base}-{quote}"
        if exchange == "gateio":
            return f"https://www.gate.io/trade/{base}_{quote}"
        if exchange == "bybit":
            return f"https://www.bybit.com/en/trade/spot/{base}/{quote}"
        if exchange == "bitget":
            return f"https://www.bitget.com/spot/{base}{quote}"
        if exchange == "coinex":
            return f"https://www.coinex.com/exchange/{base.lower()}-{quote.lower()}"
        if exchange == "bingx":
            return f"https://bingx.com/en/spot/{base}{quote}"
        return ""

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

    def format_coin_pair(self, symbol):
        """Преобразует LINKUSDT в формат LINK/USDT."""
        base, quote = self.split_symbol(symbol)
        return f"{base}/{quote}"

    def format_price(self, value):
        """Форматирует цену до 2-6 знаков без лишних нулей."""
        decimal_value = Decimal(str(value))
        formatted = f"{decimal_value:.6f}".rstrip("0").rstrip(".")
        return formatted or "0"

    def format_percent(self, value):
        """Форматирует процент в диапазоне 2-4 знака после запятой."""
        decimal_value = Decimal(str(value))
        rounded = decimal_value.quantize(Decimal("0.0001"))
        text = format(rounded, "f").rstrip("0").rstrip(".")
        if "." not in text:
            return f"{text}.00"
        fraction = text.split(".")[1]
        if len(fraction) == 1:
            return f"{text}0"
        return text

    def calculate_duration_seconds(self, start_time_iso, end_time_iso):
        """Считает длительность события в секундах."""
        start_dt = datetime.fromisoformat(start_time_iso)
        end_dt = datetime.fromisoformat(end_time_iso)
        return max(0, int((end_dt - start_dt).total_seconds()))

    def format_timestamp(self, ts):
        """Конвертирует UTC timestamp в Владивосток (UTC+10) и форматирует строку."""
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        vladivostok_tz = timezone(timedelta(hours=10))
        local_dt = dt.astimezone(vladivostok_tz)
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")

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
        """Шлёт сервисное уведомление о запуске backend."""
        startup_message = "[BACKEND] Бэкенд поднят и готов к обработке арбитража."
        try:
            await self.telegram.send_text(startup_message)
            self.backend_logger.info("Backend startup notification sent to Telegram.")
        except Exception as e:
            self.telegram.logger.error("API error while sending startup notification: %s", e)

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
        Расчёт confidence score (0–100).
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
        """Проверяет валидность агрегированной стороны стакана для расчётов."""
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

        # Получаем сырые уровни стакана для расчёта net spread
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
