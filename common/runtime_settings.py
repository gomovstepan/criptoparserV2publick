"""Управление runtime-настройками: Redis + JSON fallback."""

import json
import logging
from decimal import Decimal
from pathlib import Path


RUNTIME_SETTINGS_SCHEMA = {
    "tier_1_threshold": {"type": "decimal", "default": "0.30", "min": "0", "max": "10", "category": "Thresholds"},
    "tier_2_threshold": {"type": "decimal", "default": "0.35", "min": "0", "max": "10", "category": "Thresholds"},
    "tier_3_threshold": {"type": "decimal", "default": "0.45", "min": "0", "max": "10", "category": "Thresholds"},
    "tier_4_threshold": {"type": "decimal", "default": "0.70", "min": "0", "max": "10", "category": "Thresholds"},
    "confidence_min": {"type": "decimal", "default": "70", "min": "0", "max": "100", "category": "Thresholds"},
    "event_send_delay_seconds": {"type": "float", "default": "2", "min": "0", "max": "300", "category": "Timing"},
    "event_expire_seconds": {"type": "float", "default": "8", "min": "1", "max": "3600", "category": "Timing"},
    "render_interval_seconds": {"type": "float", "default": "0.5", "min": "0.1", "max": "60", "category": "Timing"},
    "target_value": {"type": "decimal", "default": "800", "min": "1", "max": "1000000", "category": "Timing"},
    "max_levels": {"type": "int", "default": "4", "min": "1", "max": "100", "category": "Timing"},
    "telegram_dedup_ttl_seconds": {"type": "float", "default": "60", "min": "0", "max": "3600", "category": "Telegram"},
    "withdrawal_fee_usdt": {"type": "decimal", "default": "0", "min": "0", "max": "1000", "category": "Fees"},
    "fee_binance_taker": {"type": "decimal", "default": "0.10", "min": "0", "max": "1", "category": "Fees"},
    "fee_bybit_taker": {"type": "decimal", "default": "0.10", "min": "0", "max": "1", "category": "Fees"},
    "fee_kucoin_taker": {"type": "decimal", "default": "0.10", "min": "0", "max": "1", "category": "Fees"},
    "fee_gateio_taker": {"type": "decimal", "default": "0.20", "min": "0", "max": "1", "category": "Fees"},
    "fee_bitget_taker": {"type": "decimal", "default": "0.10", "min": "0", "max": "1", "category": "Fees"},
    "fee_coinex_taker": {"type": "decimal", "default": "0.20", "min": "0", "max": "1", "category": "Fees"},
    "fee_bingx_taker": {"type": "decimal", "default": "0.10", "min": "0", "max": "1", "category": "Fees"},
}


def build_runtime_defaults(static_settings):
    """Строит словарь defaults на основе static settings из .env."""
    defaults = {}
    backend = static_settings.get("backend", {})
    fees = static_settings.get("fees", {})
    tiers = static_settings.get("tiers", {})

    defaults["tier_1_threshold"] = str(tiers.get("1", {}).get("threshold", "0.30"))
    defaults["tier_2_threshold"] = str(tiers.get("2", {}).get("threshold", "0.35"))
    defaults["tier_3_threshold"] = str(tiers.get("3", {}).get("threshold", "0.45"))
    defaults["tier_4_threshold"] = str(tiers.get("4", {}).get("threshold", "0.70"))
    defaults["confidence_min"] = str(backend.get("confidence_min", "70"))
    defaults["event_send_delay_seconds"] = str(backend.get("event_send_delay_seconds", "2"))
    defaults["event_expire_seconds"] = str(backend.get("event_expire_seconds", "8"))
    defaults["render_interval_seconds"] = str(backend.get("render_interval_seconds", "0.5"))
    defaults["target_value"] = str(backend.get("target_value", "800"))
    defaults["max_levels"] = str(backend.get("max_levels", "4"))
    defaults["telegram_dedup_ttl_seconds"] = str(backend.get("telegram_dedup_ttl_seconds", "60"))
    defaults["withdrawal_fee_usdt"] = str(backend.get("withdrawal_fee_usdt", "0"))

    for exchange in static_settings.get("exchanges", []):
        ex_fee = fees.get(exchange, {})
        defaults[f"fee_{exchange}_taker"] = str(ex_fee.get("taker", "0.10"))

    return defaults


class RuntimeSettingsStore:
    """Чтение/запись runtime-настроек через Redis с fallback на JSON-файл."""

    def __init__(self, redis_client, redis_key, file_path, static_settings):
        self.redis = redis_client
        self.redis_key = redis_key
        self.file_path = Path(file_path)
        self.static_settings = static_settings
        self.defaults = build_runtime_defaults(static_settings)
        self.logger = logging.getLogger("runtime_settings")

    async def load(self):
        """Загружает runtime-настройки: Redis → файл → defaults."""
        try:
            raw = await self.redis.hgetall(self.redis_key)
            if raw:
                decoded = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in raw.items()
                }
                merged = dict(self.defaults)
                merged.update(decoded)
                return merged
        except Exception as e:
            self.logger.error("Failed to load runtime settings from Redis: %s", e)

        # Fallback на файл
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                merged = dict(self.defaults)
                merged.update(file_data)
                return merged
            except Exception as e:
                self.logger.error("Failed to load runtime settings from file: %s", e)

        return dict(self.defaults)

    async def save(self, settings_dict):
        """Сохраняет runtime-настройки в Redis и файл."""
        # Фильтруем только известные ключи
        filtered = {k: str(v) for k, v in settings_dict.items() if k in RUNTIME_SETTINGS_SCHEMA}

        try:
            if filtered:
                await self.redis.hset(self.redis_key, mapping=filtered)
        except Exception as e:
            self.logger.error("Failed to save runtime settings to Redis: %s", e)
            raise

        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(filtered, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error("Failed to save runtime settings to file: %s", e)

    def validate(self, payload):
        """Валидирует payload runtime-настроек. Возвращает (ok: bool, errors: dict)."""
        errors = {}
        for key, value in payload.items():
            if key not in RUNTIME_SETTINGS_SCHEMA:
                errors[key] = "Unknown setting"
                continue
            schema = RUNTIME_SETTINGS_SCHEMA[key]
            try:
                if schema["type"] == "int":
                    v = int(value)
                elif schema["type"] in ("float", "decimal"):
                    v = Decimal(str(value))
                else:
                    v = value
                min_v = Decimal(schema["min"]) if schema["type"] in ("float", "decimal", "int") else schema["min"]
                max_v = Decimal(schema["max"]) if schema["type"] in ("float", "decimal", "int") else schema["max"]
                if v < min_v or v > max_v:
                    errors[key] = f"Value must be between {schema['min']} and {schema['max']}"
            except Exception:
                errors[key] = f"Invalid type, expected {schema['type']}"
        return (len(errors) == 0), errors

    def get_schema(self):
        """Возвращает JSON Schema с текущими значениями и read-only флагами."""
        schema = []
        for key, meta in RUNTIME_SETTINGS_SCHEMA.items():
            entry = dict(meta)
            entry["key"] = key
            entry["default"] = self.defaults.get(key, meta["default"])
            entry["read_only"] = False
            schema.append(entry)

        # Добавляем static read-only настройки для справки
        static_readonly = [
            {"key": "exchanges", "type": "csv", "default": ",".join(self.static_settings.get("exchanges", [])), "read_only": True, "category": "Static"},
            {"key": "redis_url", "type": "string", "default": self.static_settings.get("redis", {}).get("url", ""), "read_only": True, "category": "Static"},
            {"key": "history_db_path", "type": "string", "default": str(self.static_settings.get("backend", {}).get("history_db_path", "")), "read_only": True, "category": "Static"},
        ]
        schema.extend(static_readonly)
        return schema
