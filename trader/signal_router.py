"""Маршрутизатор торговых сигналов — подписывается на Redis Pub/Sub."""
import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as redis

from trader.models import TradeSignal, SignalType
from trader.config import TraderConfig

# Тип колбэка для обработки сигналов
SignalHandler = Callable[[TradeSignal], Awaitable[Any]]


class SignalRouter:
    """Подписывается на Redis channel 'trade:signals' и маршрутизирует сигналы."""

    def __init__(
        self,
        config: TraderConfig,
        on_open_signal: Optional[SignalHandler] = None,
        on_close_signal: Optional[SignalHandler] = None,
    ):
        self.config = config
        self.redis_client: Optional[redis.Redis] = None
        self.pubsub: Optional[redis.client.PubSub] = None
        self.logger = logging.getLogger("trader.signal_router")
        self._on_open = on_open_signal
        self._on_close = on_close_signal
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Запускает подписку на торговые сигналы из Redis Pub/Sub."""
        try:
            self.redis_client = redis.from_url(
                self.config.redis_url,
                decode_responses=True,
            )
            self.pubsub = self.redis_client.pubsub()
            await self.pubsub.subscribe(self.config.redis_channel)
            self._running = True
            self._task = asyncio.create_task(
                self._listen(), name="signal_router_listen"
            )
            self.logger.info(
                "SignalRouter запущен, слушает канал: %s",
                self.config.redis_channel,
            )
        except Exception as e:
            self.logger.error("Ошибка запуска SignalRouter: %s", e)
            raise

    async def stop(self):
        """Останавливает подписку и закрывает соединения."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.pubsub:
            try:
                await self.pubsub.unsubscribe(self.config.redis_channel)
                await self.pubsub.close()
            except Exception as e:
                self.logger.error("Ошибка закрытия pubsub: %s", e)
        if self.redis_client:
            try:
                await self.redis_client.close()
            except Exception as e:
                self.logger.error("Ошибка закрытия Redis клиента: %s", e)
        self.logger.info("SignalRouter остановлен")

    async def _listen(self):
        """Цикл прослушивания Redis Pub/Sub с обработкой сообщений."""
        while self._running:
            try:
                message = await self.pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    await self._handle_message(message["data"])
            except asyncio.CancelledError:
                self.logger.debug("Цикл прослушивания отменен")
                break
            except Exception as e:
                self.logger.error("Ошибка в цикле прослушивания: %s", e)
                await asyncio.sleep(1)

    async def _handle_message(self, data: str):
        """Обрабатывает входящий сигнал: парсит, фильтрует, маршрутизует."""
        try:
            payload = json.loads(data)
            if payload.get("type") != "trade_signal":
                # Игнорируем не-торговые сообщения
                return

            signal = TradeSignal.from_dict(payload)
            self.logger.info(
                "Получен сигнал: %s %s BUY@%s SELL@%s "
                "spread=%s%% net=%s%% conf=%s%%",
                signal.signal_type.value, signal.coin,
                signal.buy_exchange, signal.sell_exchange,
                signal.spread_percent, signal.net_spread, signal.confidence,
            )

            # Фильтруем по confidence threshold
            if signal.confidence < self.config.confidence_threshold:
                self.logger.info(
                    "Сигнал отфильтрован: confidence %s < порога %s",
                    signal.confidence, self.config.confidence_threshold
                )
                return

            # Фильтруем по min net spread
            if signal.net_spread < self.config.min_net_spread:
                self.logger.info(
                    "Сигнал отфильтрован: net_spread %s < минимума %s",
                    signal.net_spread, self.config.min_net_spread
                )
                return

            # Маршрутизируем сигнал
            if signal.signal_type == SignalType.OPEN and self._on_open:
                await self._on_open(signal)
            elif signal.signal_type == SignalType.CLOSE and self._on_close:
                await self._on_close(signal)

        except json.JSONDecodeError as e:
            self.logger.error("Некорректный JSON в сигнале: %s", e)
        except KeyError as e:
            self.logger.error("Отсутствует обязательное поле в сигнале: %s", e)
        except Exception as e:
            self.logger.error("Ошибка обработки сигнала: %s", e)

    @property
    def is_running(self) -> bool:
        """Возвращает True если роутер активен."""
        return self._running
