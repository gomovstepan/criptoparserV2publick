import logging
import os
from logging.handlers import RotatingFileHandler


MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(component)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ComponentFilter(logging.Filter):
    def __init__(self, component):
        super().__init__()
        self.component = component

    def filter(self, record):
        record.component = self.component
        return True


def _safe_level(level_name):
    allowed = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "ERROR": logging.ERROR}
    return allowed.get(level_name.upper(), logging.INFO)


def _build_handler(file_path, component):
    handler = RotatingFileHandler(
        filename=file_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(ComponentFilter(component))
    return handler


def _configure_named_logger(logger_name, file_path, component, level):
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()
    logger.addHandler(_build_handler(file_path, component))


def configure_logging(settings):
    logging_settings = settings["logging"]
    log_dir = logging_settings["dir"]
    level = _safe_level(logging_settings["level"])

    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(os.path.join(log_dir, "exchanges"), exist_ok=True)

    _configure_named_logger("backend", os.path.join(log_dir, "backend.log"), "backend", level)
    _configure_named_logger("arbitrage", os.path.join(log_dir, "arbitrage.log"), "arbitrage", level)
    _configure_named_logger("redis", os.path.join(log_dir, "redis.log"), "redis", level)
    _configure_named_logger("telegram", os.path.join(log_dir, "telegram.log"), "telegram", level)

    for exchange_name in settings["exchanges"]:
        _configure_named_logger(
            f"exchanges.{exchange_name}",
            os.path.join(log_dir, "exchanges", f"{exchange_name}.log"),
            f"exchanges.{exchange_name}",
            level,
        )
