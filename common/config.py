import os
from decimal import Decimal

from dotenv import load_dotenv


ENV_LOADED = False


def get_env(name, default=None, required=False):
    """Возвращает значение переменной окружения с optional-проверкой обязательности."""
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


def parse_csv(value):
    """Преобразует CSV-строку в список непустых значений без пробелов по краям."""
    return [item.strip() for item in value.split(",") if item.strip()]


def load_settings():
    """Собирает и валидирует runtime-конфигурацию приложения из переменных окружения."""
    global ENV_LOADED

    if not ENV_LOADED:
        load_dotenv()
        ENV_LOADED = True

    exchanges = parse_csv(get_env("EXCHANGES", required=True))
    default_poll_interval = float(get_env("EXCHANGE_POLL_INTERVAL_SECONDS", default="1"))

    settings = {
        "exchanges": exchanges,
        "api_key": get_env("API_KEY", default=""),
        "logging": {
            "level": get_env("LOG_LEVEL", default="INFO").upper(),
            "dir": get_env("LOG_DIR", default="/app/logs"),
        },
        "redis": {
            "url": get_env("REDIS_URL", required=True),
            "key_prefix": get_env("ORDERBOOK_REDIS_KEY_PREFIX", required=True),
            "symbols_set_template": get_env("ORDERBOOK_REDIS_SYMBOLS_SET_TEMPLATE", required=True),
            "arbitrage_events_set_key": get_env(
                "ARBITRAGE_EVENTS_REDIS_SET_KEY",
                default="arbitrage:events:active",
            ),
            "arbitrage_event_key_template": get_env(
                "ARBITRAGE_EVENT_REDIS_KEY_TEMPLATE",
                default="arbitrage:event:{event_key}",
            ),
            "runtime_settings_key": get_env(
                "RUNTIME_SETTINGS_REDIS_KEY",
                default="runtime:settings",
            ),
        },
        "backend": {
            "render_interval_seconds": float(get_env("BACKEND_RENDER_INTERVAL_SECONDS", required=True)),
            "target_value": Decimal(get_env("ORDERBOOK_TARGET_VALUE", required=True)),
            "max_levels": int(get_env("ORDERBOOK_MAX_LEVELS", required=True)),
            "arbitrage_min_spread_percent": Decimal(
                get_env(
                    "ARBITRAGE_MIN_SPREAD_PERCENT",
                    default=get_env("SPREAD_THRESHOLD_PCT", default="0.3"),
                )
            ),
            "event_send_delay_seconds": float(get_env("EVENT_SEND_DELAY_SECONDS", default="2")),
            "event_expire_seconds": float(get_env("EVENT_EXPIRE_SECONDS", default="8")),
            "history_limit": int(get_env("ARBITRAGE_HISTORY_LIMIT", default="1000")),
            "history_db_path": get_env("ARBITRAGE_HISTORY_DB_PATH", default="/data/arbitrage_history.db"),
            "confidence_min": Decimal(get_env("CONFIDENCE_MIN", default="70")),
            "withdrawal_fee_usdt": Decimal(get_env("WITHDRAWAL_FEE_USDT", default="0")),
            "runtime_settings_path": get_env("RUNTIME_SETTINGS_PATH", default="/data/runtime_settings.json"),
        },
        "telegram": {
            "bot_token": get_env("TELEGRAM_BOT_TOKEN", default=""),
            "chat_id": get_env("TELEGRAM_CHAT_ID", default=""),
        },
        "exchange_configs": {},
        "fees": {},
        "tiers": {},
    }

    # --- Fee configuration ---
    for ex in exchanges:
        ex_upper = ex.upper()
        taker = Decimal(get_env(f"EXCHANGE_FEE_{ex_upper}", default="0.10"))
        maker = Decimal(get_env(f"EXCHANGE_MAKER_FEE_{ex_upper}", default=str(taker)))
        token_discount = Decimal(get_env(f"EXCHANGE_TOKEN_DISCOUNT_{ex_upper}", default="0"))
        settings["fees"][ex] = {
            "taker": taker,
            "maker": maker,
            "token_discount": token_discount,
        }

    # --- Tier configuration ---
    settings["tiers"] = {
        "1": {
            "pairs": parse_csv(get_env("TIER_1_PAIRS", default="BTCUSDT,ETHUSDT")),
            "threshold": Decimal(get_env("TIER_1_THRESHOLD", default="0.30")),
        },
        "2": {
            "pairs": parse_csv(get_env("TIER_2_PAIRS", default="")),
            "threshold": Decimal(get_env("TIER_2_THRESHOLD", default="0.35")),
        },
        "3": {
            "pairs": parse_csv(get_env("TIER_3_PAIRS", default="")),
            "threshold": Decimal(get_env("TIER_3_THRESHOLD", default="0.45")),
        },
        "4": {
            "pairs": parse_csv(get_env("TIER_4_PAIRS", default="")),
            "threshold": Decimal(get_env("TIER_4_THRESHOLD", default="0.70")),
        },
    }

    if "bybit" in exchanges:
        settings["exchange_configs"]["bybit"] = {
            "exchange_name": get_env("BYBIT_EXCHANGE_NAME", default="bybit"),
            "rest_orderbook_url": get_env("BYBIT_REST_ORDERBOOK_URL", required=True),
            "ws_url": get_env("BYBIT_WS_URL", default="wss://stream.bybit.com/v5/public/spot"),
            "use_websocket": get_env("BYBIT_USE_WEBSOCKET", default="false").lower() == "true",
            "category": get_env("BYBIT_CATEGORY", default="spot"),
            "symbols": parse_csv(get_env("BYBIT_SYMBOLS", required=True)),
            "orderbook_depth": int(get_env("BYBIT_ORDERBOOK_DEPTH", required=True)),
            "poll_interval_seconds": float(get_env("BYBIT_POLL_INTERVAL_SECONDS", default=str(default_poll_interval))),
            "reconnect_delay_seconds": float(get_env("BYBIT_RECONNECT_DELAY_SECONDS", required=True)),
            "max_symbols_per_connection": int(get_env("BYBIT_MAX_SYMBOLS_PER_CONNECTION", default=get_env("BYBIT_MAX_TOPICS_PER_CONNECTION", default="10"))),
            "max_concurrent_requests": int(get_env("BYBIT_MAX_CONCURRENT_REQUESTS", default="10")),
        }

    if "binance" in exchanges:
        settings["exchange_configs"]["binance"] = {
            "exchange_name": get_env("BINANCE_EXCHANGE_NAME", default="binance"),
            "rest_orderbook_url": get_env("BINANCE_REST_ORDERBOOK_URL", required=True),
            "ws_url": get_env("BINANCE_WS_URL", default="wss://stream.binance.com:9443/ws"),
            "use_websocket": get_env("BINANCE_USE_WEBSOCKET", default="false").lower() == "true",
            "symbols": parse_csv(get_env("BINANCE_SYMBOLS", required=True)),
            "orderbook_depth": int(get_env("BINANCE_ORDERBOOK_DEPTH", required=True)),
            "poll_interval_seconds": float(get_env("BINANCE_POLL_INTERVAL_SECONDS", default=str(default_poll_interval))),
            "reconnect_delay_seconds": float(get_env("BINANCE_RECONNECT_DELAY_SECONDS", required=True)),
            "max_symbols_per_connection": int(get_env("BINANCE_MAX_SYMBOLS_PER_CONNECTION", default=get_env("BINANCE_MAX_STREAMS_PER_CONNECTION", default="200"))),
            "max_concurrent_requests": int(get_env("BINANCE_MAX_CONCURRENT_REQUESTS", default="10")),
        }

    if "kucoin" in exchanges:
        settings["exchange_configs"]["kucoin"] = {
            "exchange_name": get_env("KUCOIN_EXCHANGE_NAME", default="kucoin"),
            "rest_orderbook_url": get_env("KUCOIN_REST_ORDERBOOK_URL", required=True),
            "ws_url": get_env("KUCOIN_WS_URL", default="wss://ws-api-spot.kucoin.com"),
            "bullet_public_url": get_env("KUCOIN_BULLET_PUBLIC_URL", default="https://api.kucoin.com/api/v1/bullet-public"),
            "use_websocket": get_env("KUCOIN_USE_WEBSOCKET", default="false").lower() == "true",
            "symbols": parse_csv(get_env("KUCOIN_SYMBOLS", required=True)),
            "poll_interval_seconds": float(get_env("KUCOIN_POLL_INTERVAL_SECONDS", default=str(default_poll_interval))),
            "reconnect_delay_seconds": float(get_env("KUCOIN_RECONNECT_DELAY_SECONDS", required=True)),
            "max_symbols_per_connection": int(get_env("KUCOIN_MAX_SYMBOLS_PER_CONNECTION", required=True)),
            "max_concurrent_requests": int(get_env("KUCOIN_MAX_CONCURRENT_REQUESTS", default="10")),
        }

    if "bitget" in exchanges:
        settings["exchange_configs"]["bitget"] = {
            "exchange_name": get_env("BITGET_EXCHANGE_NAME", default="bitget"),
            "rest_orderbook_url": get_env("BITGET_REST_ORDERBOOK_URL", required=True),
            "ws_url": get_env("BITGET_WS_URL", default="wss://ws.bitget.com/v2/ws/public"),
            "use_websocket": get_env("BITGET_USE_WEBSOCKET", default="false").lower() == "true",
            "symbols": parse_csv(get_env("BITGET_SYMBOLS", required=True)),
            "book_type": get_env("BITGET_BOOK_TYPE", default="step0"),
            "orderbook_depth": int(get_env("BITGET_ORDERBOOK_DEPTH", required=True)),
            "poll_interval_seconds": float(get_env("BITGET_POLL_INTERVAL_SECONDS", default=str(default_poll_interval))),
            "reconnect_delay_seconds": float(get_env("BITGET_RECONNECT_DELAY_SECONDS", required=True)),
            "max_symbols_per_connection": int(get_env("BITGET_MAX_SYMBOLS_PER_CONNECTION", required=True)),
            "max_concurrent_requests": int(get_env("BITGET_MAX_CONCURRENT_REQUESTS", default="10")),
        }

    if "coinex" in exchanges:
        settings["exchange_configs"]["coinex"] = {
            "exchange_name": get_env("COINEX_EXCHANGE_NAME", default="coinex"),
            "rest_orderbook_url": get_env("COINEX_REST_ORDERBOOK_URL", required=True),
            "ws_url": get_env("COINEX_WS_URL", default="wss://socket.coinex.com/"),
            "use_websocket": get_env("COINEX_USE_WEBSOCKET", default="false").lower() == "true",
            "symbols": parse_csv(get_env("COINEX_SYMBOLS", required=True)),
            "depth_limit": int(get_env("COINEX_DEPTH_LIMIT", required=True)),
            "price_interval": get_env("COINEX_PRICE_INTERVAL", required=True),
            "poll_interval_seconds": float(get_env("COINEX_POLL_INTERVAL_SECONDS", default=str(default_poll_interval))),
            "reconnect_delay_seconds": float(get_env("COINEX_RECONNECT_DELAY_SECONDS", required=True)),
            "max_symbols_per_connection": int(get_env("COINEX_MAX_SYMBOLS_PER_CONNECTION", required=True)),
            "max_concurrent_requests": int(get_env("COINEX_MAX_CONCURRENT_REQUESTS", default="10")),
        }

    if "bingx" in exchanges:
        settings["exchange_configs"]["bingx"] = {
            "exchange_name": get_env("BINGX_EXCHANGE_NAME", default="bingx"),
            "rest_orderbook_url": get_env("BINGX_REST_ORDERBOOK_URL", required=True),
            "ws_url": get_env("BINGX_WS_URL", default="wss://open-api-ws.bingx.com/market"),
            "use_websocket": get_env("BINGX_USE_WEBSOCKET", default="false").lower() == "true",
            "symbols": parse_csv(get_env("BINGX_SYMBOLS", required=True)),
            "orderbook_depth": int(get_env("BINGX_ORDERBOOK_DEPTH", required=True)),
            "poll_interval_seconds": float(get_env("BINGX_POLL_INTERVAL_SECONDS", default=str(default_poll_interval))),
            "reconnect_delay_seconds": float(get_env("BINGX_RECONNECT_DELAY_SECONDS", required=True)),
            "max_symbols_per_connection": int(get_env("BINGX_MAX_SYMBOLS_PER_CONNECTION", required=True)),
            "max_concurrent_requests": int(get_env("BINGX_MAX_CONCURRENT_REQUESTS", default="10")),
        }

    if "gateio" in exchanges:
        settings["exchange_configs"]["gateio"] = {
            "exchange_name": get_env("GATEIO_EXCHANGE_NAME", default="gateio"),
            "rest_url": get_env("GATEIO_REST_ORDERBOOK_URL", required=True),
            "symbols": parse_csv(get_env("GATEIO_SYMBOLS", required=True)),
            "rest_snapshot_limit": int(get_env("GATEIO_REST_SNAPSHOT_LIMIT", required=True)),
            "poll_interval_seconds": float(get_env("GATEIO_POLL_INTERVAL_SECONDS", default=str(default_poll_interval))),
            "reconnect_delay_seconds": float(get_env("GATEIO_RECONNECT_DELAY_SECONDS", required=True)),
            "max_symbols_per_connection": int(get_env("GATEIO_MAX_SYMBOLS_PER_CONNECTION", required=True)),
            "max_concurrent_requests": int(get_env("GATEIO_MAX_CONCURRENT_REQUESTS", default="10")),
        }

    return settings
