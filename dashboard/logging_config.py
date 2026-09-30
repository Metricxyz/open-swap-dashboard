from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .rpc import sanitize_error


MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record):
        return sanitize_error(super().format(record))


def configure_logging(data_dir: Path) -> Path:
    level_name = os.getenv("OPEN_METRIC_DASHBOARD_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    formatter = _UtcFormatter(
        "%(asctime)sZ %(levelname)s %(name)s [%(threadName)s] %(message)s",
        "%Y-%m-%dT%H:%M:%S",
    )
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    app_logger = logging.getLogger("dashboard")
    app_logger.setLevel(level)

    log_path = Path(data_dir) / "logs" / "open-metric-dashboard.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(app_logger.handlers):
        if getattr(handler, "_open_metric_dashboard_file", False):
            app_logger.removeHandler(handler)
            handler.close()
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler._open_metric_dashboard_file = True
    file_handler.setFormatter(formatter)
    app_logger.addHandler(file_handler)
    app_logger.info(
        "logging configured file=%s max_bytes=%s backups=%s level=%s",
        log_path, MAX_LOG_BYTES, LOG_BACKUP_COUNT, logging.getLevelName(level),
    )
    return log_path
