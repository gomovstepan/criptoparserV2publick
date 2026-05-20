"""Конфигурация торгового модуля из переменных окружения."""
import os
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def _getenv_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key, "")
    return value.lower() in ("true", "1", "yes", "on") if value else default


def _getenv_decimal(key: str, default: str = "0") -> Decimal:
    return Decimal(str(os.getenv(key, default)))


def _parse_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


@dataclass
class TraderConfig:
    """Конфигурация торгового сервиса."""

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"
    redis_channel: str = "trade:signals"

    # --- Общие торговые настройки ---
    # Торговый режим: demo | paper | real (пока реализованы demo и paper)
    trade_mode: str = "demo"

    # Процент от баланса на одну сделку (Q2: вариант Б)
    trade_balance_percent: Decimal = Decimal("10")

    # Минимальный USDT баланс для сделки
    min_trade_amount_usdt: Decimal = Decimal("50")

    # Confidence threshold для входа
    confidence_threshold: Decimal = Decimal("70")

    # Минимальный net_spread для входа (%)
    min_net_spread: Decimal = Decimal("0.3")

    # --- Ребалансировка (Q3: вариант В) ---
    # Включить автоматическую ребалансировку
    rebalance_enabled: bool = True

    # Порог разницы балансов (USDT) для запуска ребаланса
    rebalance_threshold_usdt: Decimal = Decimal("1000")

    # Минимальная сумма перевода (USDT)
    rebalance_min_transfer_usdt: Decimal = Decimal("100")

    # Интервал проверки ребаланса (сек)
    rebalance_check_interval_seconds: int = 300

    # --- Биржи для demo торговли (Q4) ---
    demo_exchanges: List[str] = field(default_factory=lambda: ["bybit", "binance", "kucoin", "bitget"])

    # --- API Keys (только testnet/demo!) ---
    # Bybit testnet
    bybit_testnet_api_key: str = ""
    bybit_testnet_api_secret: str = ""

    # Binance testnet
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""

    # KuCoin paper trade
    kucoin_paper_api_key: str = ""
    kucoin_paper_api_secret: str = ""
    kucoin_paper_api_passphrase: str = ""

    # Bitget demo
    bitget_demo_api_key: str = ""
    bitget_demo_api_secret: str = ""
    bitget_demo_api_passphrase: str = ""

    # --- Балансы для paper trading (имитация) ---
    # Начальный баланс USDT на каждой бирже для paper trading
    paper_balance_usdt: Decimal = Decimal("10000")

    # --- База данных истории ---
    history_db_path: str = "/data/trade_history.db"

    # --- Логирование ---
    log_level: str = "INFO"
    log_dir: str = "/app/logs"

    @classmethod
    def from_env(cls) -> "TraderConfig":
        """Загружает конфигурацию из переменных окружения."""
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            redis_channel=os.getenv("TRADER_REDIS_CHANNEL", "trade:signals"),
            trade_mode=os.getenv("TRADER_MODE", "demo"),
            trade_balance_percent=_getenv_decimal("TRADER_BALANCE_PERCENT", "10"),
            min_trade_amount_usdt=_getenv_decimal("TRADER_MIN_AMOUNT_USDT", "50"),
            confidence_threshold=_getenv_decimal("TRADER_CONFIDENCE_THRESHOLD", "70"),
            min_net_spread=_getenv_decimal("TRADER_MIN_NET_SPREAD", "0.3"),
            rebalance_enabled=_getenv_bool("TRADER_REBALANCE_ENABLED", True),
            rebalance_threshold_usdt=_getenv_decimal("TRADER_REBALANCE_THRESHOLD_USDT", "1000"),
            rebalance_min_transfer_usdt=_getenv_decimal("TRADER_REBALANCE_MIN_TRANSFER", "100"),
            rebalance_check_interval_seconds=int(os.getenv("TRADER_REBALANCE_INTERVAL", "300")),
            demo_exchanges=_parse_list(os.getenv("TRADER_DEMO_EXCHANGES", "bybit,binance,kucoin,bitget")),
            bybit_testnet_api_key=os.getenv("BYBIT_TESTNET_API_KEY", ""),
            bybit_testnet_api_secret=os.getenv("BYBIT_TESTNET_API_SECRET", ""),
            binance_testnet_api_key=os.getenv("BINANCE_TESTNET_API_KEY", ""),
            binance_testnet_api_secret=os.getenv("BINANCE_TESTNET_API_SECRET", ""),
            kucoin_paper_api_key=os.getenv("KUCOIN_PAPER_API_KEY", ""),
            kucoin_paper_api_secret=os.getenv("KUCOIN_PAPER_API_SECRET", ""),
            kucoin_paper_api_passphrase=os.getenv("KUCOIN_PAPER_API_PASSPHRASE", ""),
            bitget_demo_api_key=os.getenv("BITGET_DEMO_API_KEY", ""),
            bitget_demo_api_secret=os.getenv("BITGET_DEMO_API_SECRET", ""),
            bitget_demo_api_passphrase=os.getenv("BITGET_DEMO_API_PASSPHRASE", ""),
            paper_balance_usdt=_getenv_decimal("TRADER_PAPER_BALANCE_USDT", "10000"),
            history_db_path=os.getenv("TRADER_HISTORY_DB_PATH", "/data/trade_history.db"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_dir=os.getenv("LOG_DIR", "/app/logs"),
        )

    @property
    def is_demo(self) -> bool:
        return self.trade_mode == "demo"

    @property
    def is_paper(self) -> bool:
        return self.trade_mode == "paper"

    @property
    def is_real(self) -> bool:
        return self.trade_mode == "real"

    def get_exchange_credentials(self, exchange: str) -> Dict[str, str]:
        """Возвращает API-кредитлы для биржи."""
        exchange = exchange.lower()
        if exchange == "bybit":
            return {
                "api_key": self.bybit_testnet_api_key,
                "api_secret": self.bybit_testnet_api_secret,
            }
        elif exchange == "binance":
            return {
                "api_key": self.binance_testnet_api_key,
                "api_secret": self.binance_testnet_api_secret,
            }
        elif exchange == "kucoin":
            return {
                "api_key": self.kucoin_paper_api_key,
                "api_secret": self.kucoin_paper_api_secret,
                "passphrase": self.kucoin_paper_api_passphrase,
            }
        elif exchange == "bitget":
            return {
                "api_key": self.bitget_demo_api_key,
                "api_secret": self.bitget_demo_api_secret,
                "passphrase": self.bitget_demo_api_passphrase,
            }
        return {}
