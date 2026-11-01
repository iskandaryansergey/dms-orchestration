"""Structured JSON logging that never prints task tokens or secret values."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

SENSITIVE_KEYS = {"taskToken", "TaskToken", "token", "password", "Password", "SecretString", "secret"}


def mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k in SENSITIVE_KEYS else mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {"level": record.levelname, "message": record.getMessage(), "logger": record.name}
        extra = getattr(record, "fields", None)
        if extra:
            data.update(mask(extra))
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def info(logger: logging.Logger, message: str, **fields: Any) -> None:
    logger.info(message, extra={"fields": fields})


def warn(logger: logging.Logger, message: str, **fields: Any) -> None:
    logger.warning(message, extra={"fields": fields})


def metric(logger: logging.Logger, name: str, value: float = 1, unit: str = "Count", **dims: str) -> None:
    """Emit a CloudWatch Embedded Metric Format line (namespace DmsOrchestration)."""
    import time

    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "DmsOrchestration",
                    "Dimensions": [sorted(dims)],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        **dims,
    }
    print(json.dumps(payload))
