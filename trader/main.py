"""Точка входа торгового сервиса (отдельный контейнер)."""
import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.config import TraderConfig
from trader.exchange_clients.base import BaseExchangeClient
from trader.exchange_clients.paper_client import PaperExchangeClient
from trader.signal_router import SignalRouter
from trader.position_manager import PositionManager
from trader.balance_tracker import BalanceTracker
from trader.rebalance_engine import RebalanceEngine
from trader.pnl_calculator import PnLCalculator
from trader.history_store import TradeHistoryStore

# Импорт клиентов для demo-режима (опциональные — fallback на paper)
try:
    from trader.exchange_clients.bybit_client import BybitTestnetClient
except ImportError:
    BybitTestnetClient = None

try:
    from trader.exchange_clients.binance_client import BinanceTestnetClient
except ImportError:
    BinanceTestnetClient = None

try:
    from trader.exchange_clients.kucoin_client import KuCoinPaperClient
except ImportError:
    KuCoinPaperClient = None

try:
    from trader.exchange_clients.bitget_client import BitgetDemoClient
except ImportError:
    BitgetDemoClient = None


class TraderService:
    """Основной сервис торгового бота."""

    def __init__(self):
        self.config = TraderConfig.from_env()
        self._setup_logging()
        self.logger = logging.getLogger("trader")
        self.exchange_clients: dict = {}
        self.signal_router: Optional[SignalRouter] = None
        self.position_manager: Optional[PositionManager] = None
        self.balance_tracker: Optional[BalanceTracker] = None
        self.rebalance_engine: Optional[RebalanceEngine] = None
        self.pnl_calculator: Optional[PnLCalculator] = None
        self.history_store: Optional[TradeHistoryStore] = None
        self._shutdown_event = asyncio.Event()

    def _setup_logging(self):
        """Настраивает логирование."""
        os.makedirs(self.config.log_dir, exist_ok=True)
        logging.basicConfig(
            level=getattr(logging, self.config.log_level),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(
                    os.path.join(self.config.log_dir, "trader.log")
                ),
            ],
        )

    async def _create_exchange_clients(self):
        """Создает клиенты для всех бирж в зависимости от режима торговли."""
        all_exchanges = ["bybit", "binance", "kucoin", "bitget", "gateio", "coinex", "bingx"]

        for exchange in all_exchanges:
            client = await self._create_client_for_exchange(exchange)
            if client:
                self.exchange_clients[exchange] = client
                self.logger.info(
                    "Exchange client ready: %s (%s)", exchange, type(client).__name__
                )

        if not self.exchange_clients:
            self.logger.warning("No exchange clients available!")

    async def _create_client_for_exchange(self, exchange: str):
        """Создает подходящий клиент для биржи."""
        client = None

        if self.config.is_paper:
            # Paper trading для всех бирж
            client = PaperExchangeClient(
                exchange_name=exchange,
                initial_usdt=self.config.paper_balance_usdt,
            )
        elif self.config.is_demo:
            # Demo для бирж с testnet, paper для остальных
            credentials = self.config.get_exchange_credentials(exchange)

            if exchange == "bybit" and BybitTestnetClient and credentials.get("api_key"):
                client = BybitTestnetClient(credentials)
            elif exchange == "binance" and BinanceTestnetClient and credentials.get("api_key"):
                client = BinanceTestnetClient(credentials)
            elif exchange == "kucoin" and KuCoinPaperClient and credentials.get("api_key"):
                client = KuCoinPaperClient(credentials)
            elif exchange == "bitget" and BitgetDemoClient and credentials.get("api_key"):
                client = BitgetDemoClient(credentials)
            else:
                client = PaperExchangeClient(
                    exchange_name=exchange,
                    initial_usdt=self.config.paper_balance_usdt,
                )

        if client:
            try:
                connected = await client.connect()
                if connected:
                    return client
                else:
                    self.logger.error("Failed to connect %s, using paper fallback", exchange)
            except Exception as e:
                self.logger.error("Error connecting %s: %s, using paper fallback", exchange, e)

            # Fallback на paper client
            paper = PaperExchangeClient(
                exchange_name=exchange,
                initial_usdt=self.config.paper_balance_usdt,
            )
            try:
                await paper.connect()
                return paper
            except Exception as e:
                self.logger.error("Paper fallback also failed for %s: %s", exchange, e)
                return None

        return None

    async def _on_open_signal(self, signal):
        """Обработчик OPEN сигнала."""
        self.logger.info(
            "=== OPEN SIGNAL: %s BUY@%s SELL@%s spread=%s%% net=%s%% ===",
            signal.coin,
            signal.buy_exchange,
            signal.sell_exchange,
            signal.spread_percent,
            signal.net_spread,
        )

        # Проверяем что клиенты доступны для обеих бирж
        if signal.buy_exchange not in self.exchange_clients:
            self.logger.error("No client for buy exchange: %s", signal.buy_exchange)
            return
        if signal.sell_exchange not in self.exchange_clients:
            self.logger.error("No client for sell exchange: %s", signal.sell_exchange)
            return

        # Проверяем минимальный net_spread
        if signal.net_spread < self.config.min_net_spread:
            self.logger.info(
                "Signal net_spread %.4f%% below threshold %.4f%%, skipping",
                signal.net_spread,
                self.config.min_net_spread,
            )
            return

        # Проверяем confidence threshold
        if signal.confidence < self.config.confidence_threshold:
            self.logger.info(
                "Signal confidence %.1f below threshold %.1f, skipping",
                signal.confidence,
                self.config.confidence_threshold,
            )
            return

        # Открываем позицию
        position = await self.position_manager.open_position(
            signal, self.exchange_clients
        )
        if position:
            # Потенциальный PnL
            potential = self.pnl_calculator.calculate_potential_pnl(
                signal, position.target_value_usdt
            )
            self.logger.info(
                "Position opened: id=%s | Potential PnL: %s USDT (%.4f%%)",
                position.id,
                potential.get("net_pnl", Decimal("0")),
                potential.get("net_pnl_percent", Decimal("0")),
            )

    async def _on_close_signal(self, signal):
        """Обработчик CLOSE сигнала."""
        self.logger.info(
            "=== CLOSE SIGNAL: %s BUY@%s SELL@%s ===",
            signal.coin,
            signal.buy_exchange,
            signal.sell_exchange,
        )

        position = await self.position_manager.close_position(
            signal, self.exchange_clients
        )
        if position:
            self.logger.info(
                "Position closed: id=%s | PnL=%s USDT (%.4f%%) | duration=%ss",
                position.id,
                position.actual_pnl,
                position.actual_pnl_percent,
                position.duration_seconds,
            )

            # Сравниваем потенциальный и фактический PnL
            comparison = self.pnl_calculator.compare_potential_vs_actual(position)
            self.logger.info(
                "PnL comparison — Potential: %s | Actual: %s | Slippage: %s (%.1f%%)",
                comparison.get("potential_pnl", Decimal("0")),
                comparison.get("actual_pnl", Decimal("0")),
                comparison.get("slippage", Decimal("0")),
                comparison.get("slippage_percent", Decimal("0")),
            )

    async def start(self):
        """Запускает все компоненты сервиса."""
        self.logger.info(
            "=== TraderService starting (mode=%s) ===", self.config.trade_mode
        )

        # История сделок и балансов
        self.history_store = TradeHistoryStore(self.config.history_db_path)

        # Клиенты бирж
        await self._create_exchange_clients()

        if not self.exchange_clients:
            self.logger.error("No exchange clients available, cannot start")
            return

        # Основные компоненты
        self.position_manager = PositionManager(self.config, self.history_store)
        self.balance_tracker = BalanceTracker(
            self.config, self.exchange_clients, self.history_store
        )
        self.rebalance_engine = RebalanceEngine(
            self.config, self.balance_tracker, self.history_store
        )
        self.pnl_calculator = PnLCalculator(self.config, self.history_store)

        # Роутер торговых сигналов
        self.signal_router = SignalRouter(
            self.config,
            on_open_signal=self._on_open_signal,
            on_close_signal=self._on_close_signal,
        )

        # Запуск компонентов
        await self.balance_tracker.start()
        await self.rebalance_engine.start()
        await self.signal_router.start()

        self.logger.info(
            "=== TraderService ready (%d exchanges) ===",
            len(self.exchange_clients),
        )

        # Ждем сигнала завершения
        await self._shutdown_event.wait()

    async def stop(self):
        """Останавливает все компоненты."""
        self.logger.info("=== TraderService shutting down ===")
        self._shutdown_event.set()

        if self.signal_router:
            try:
                await self.signal_router.stop()
            except Exception as e:
                self.logger.error("Error stopping signal router: %s", e)

        if self.rebalance_engine:
            try:
                await self.rebalance_engine.stop()
            except Exception as e:
                self.logger.error("Error stopping rebalance engine: %s", e)

        if self.balance_tracker:
            try:
                await self.balance_tracker.stop()
            except Exception as e:
                self.logger.error("Error stopping balance tracker: %s", e)

        # Закрываем клиенты бирж
        for name, client in self.exchange_clients.items():
            try:
                await client.close()
                self.logger.info("Closed client: %s", name)
            except Exception as e:
                self.logger.error("Error closing %s: %s", name, e)

        # Финальный отчет по PnL
        if self.pnl_calculator:
            try:
                report = self.pnl_calculator.get_weekly_report()
                formatted = self.pnl_calculator.format_report(report)
                self.logger.info("\n%s", formatted)
            except Exception as e:
                self.logger.error("Error generating final report: %s", e)

        self.logger.info("=== TraderService stopped ===")


def main():
    """Точка входа."""
    service = TraderService()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Обработка системных сигналов для graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda s=sig: asyncio.create_task(service.stop())
        )

    try:
        loop.run_until_complete(service.start())
    except KeyboardInterrupt:
        pass
    finally:
        # Гарантированная остановка
        if not service._shutdown_event.is_set():
            loop.run_until_complete(service.stop())
        loop.close()


if __name__ == "__main__":
    main()
