"""Биржевые клиенты для торгового модуля.

Пакет содержит реализации клиентов для различных бирж:
- PaperExchangeClient: Внутренний paper trading (без API ключей)
- BybitTestnetClient: Bybit testnet API
- BinanceTestnetClient: Binance spot testnet API
- KuCoinPaperClient: KuCoin paper trading API
- BitgetDemoClient: Bitget demo trading API
"""

from trader.exchange_clients.base import BaseExchangeClient
from trader.exchange_clients.paper_client import PaperExchangeClient
from trader.exchange_clients.bybit_client import BybitTestnetClient
from trader.exchange_clients.binance_client import BinanceTestnetClient
from trader.exchange_clients.kucoin_client import KuCoinPaperClient
from trader.exchange_clients.bitget_client import BitgetDemoClient

__all__ = [
    "BaseExchangeClient",
    "PaperExchangeClient",
    "BybitTestnetClient",
    "BinanceTestnetClient",
    "KuCoinPaperClient",
    "BitgetDemoClient",
]
