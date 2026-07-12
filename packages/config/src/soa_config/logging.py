"""Structured logging, correlation context, and redaction helpers.

Every process logs single-line JSON with a stable envelope: timestamp,
level, logger, message, service, environment, correlation_id, and any extra
fields. Extra fields pass through key-based redaction so sensitive values
never reach log transport even when a call site forgets to mask them.

Correlation IDs live in a ``ContextVar`` so one ID set at the request or job
boundary appears on every log record emitted underneath it — including audit
and job records created later in the call stack.
"""

import json
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PATTERN = re.compile(
    r"password|secret|token|api[_-]?key|authorization|credential|private[_-]?key"
    r"|database[_-]?url|dsn|cookie|session[_-]?id",
    re.IGNORECASE,
)

_correlation_id: ContextVar[str | None] = ContextVar("soa_correlation_id", default=None)

# Attributes present on every LogRecord; anything else was passed via extra=.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(correlation_id: str | None) -> None:
    _correlation_id.set(correlation_id)


@contextmanager
def correlation_context(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation ID for the duration of a request or job."""
    resolved = correlation_id or new_correlation_id()
    token = _correlation_id.set(resolved)
    try:
        yield resolved
    finally:
        _correlation_id.reset(token)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(key))


def redact_value(key: str, value: Any) -> Any:
    """Redact by key, recursing into nested mappings and sequences."""
    if is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(key, item) for item in value]
    return value


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: redact_value(key, value) for key, value in mapping.items()}


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service_name,
            "environment": self._environment,
            "correlation_id": get_correlation_id(),
        }
        extras = {
            key: value for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS
        }
        if extras:
            payload.update(redact_mapping(extras))
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
            }
        return json.dumps(payload, default=str)


def configure_logging(*, service_name: str, environment: str, level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name=service_name, environment=environment))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
