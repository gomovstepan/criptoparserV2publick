"""Общие утилиты, используемые несколькими компонентами."""


def normalize_symbol(symbol: str) -> str:
    """Нормализует символ: uppercase + только буквы/цифры.

    Примеры:
        - btc-usdt  -> BTCUSDT
        - BTC_USDT  -> BTCUSDT
        - btcusdt   -> BTCUSDT
    """
    return "".join(ch for ch in symbol.upper() if ch.isalnum())
