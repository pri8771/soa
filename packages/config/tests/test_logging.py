import json
import logging

import pytest

from soa_config.logging import (
    REDACTED,
    JsonFormatter,
    configure_logging,
    correlation_context,
    get_correlation_id,
    redact_mapping,
)


def make_record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def format_record(record: logging.LogRecord) -> dict[str, object]:
    formatter = JsonFormatter(service_name="soa-test", environment="test")
    return json.loads(formatter.format(record))


def test_json_envelope_has_stable_fields() -> None:
    payload = format_record(make_record("hello"))
    assert payload["message"] == "hello"
    assert payload["service"] == "soa-test"
    assert payload["environment"] == "test"
    assert payload["level"] == "INFO"
    assert "timestamp" in payload
    assert payload["correlation_id"] is None


def test_correlation_id_appears_on_records_inside_context() -> None:
    with correlation_context("corr-abc") as cid:
        assert cid == "corr-abc"
        assert get_correlation_id() == "corr-abc"
        payload = format_record(make_record("inside"))
    assert payload["correlation_id"] == "corr-abc"
    assert get_correlation_id() is None


def test_correlation_context_generates_id_when_absent() -> None:
    with correlation_context() as cid:
        assert cid
        assert get_correlation_id() == cid


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "user_password",
        "SECRET_KEY",
        "api_key",
        "Authorization",
        "database_url",
        "refresh_token",
        "session_id",
    ],
)
def test_sensitive_extra_keys_are_redacted(key: str) -> None:
    payload = format_record(make_record("event", **{key: "super-sensitive"}))
    assert payload[key] == REDACTED
    assert "super-sensitive" not in json.dumps(payload)


def test_redaction_recurses_into_nested_structures() -> None:
    redacted = redact_mapping(
        {
            "outer": {"password": "pw", "safe": "ok"},
            "items": [{"token": "t1"}, {"note": "fine"}],
        }
    )
    assert redacted["outer"]["password"] == REDACTED
    assert redacted["outer"]["safe"] == "ok"
    assert redacted["items"][0]["token"] == REDACTED
    assert redacted["items"][1]["note"] == "fine"


def test_non_serializable_extras_do_not_crash_formatter() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque-object"

    payload = format_record(make_record("event", context=Opaque()))
    assert payload["context"] == "opaque-object"


def test_exception_info_records_type_and_message() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=True,
        )
        import sys

        record.exc_info = sys.exc_info()
    payload = format_record(record)
    exception = payload["exception"]
    assert isinstance(exception, dict)
    assert exception["type"] == "ValueError"


def test_configure_logging_is_idempotent() -> None:
    configure_logging(service_name="soa-test", environment="test")
    configure_logging(service_name="soa-test", environment="test")
    assert len(logging.getLogger().handlers) == 1
    logging.getLogger().handlers = []
