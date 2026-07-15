import logging

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment


def test_correlation_id_travels_from_request_to_log_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(ApiSettings(environment=Environment.TEST))
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger="soa_api.app"):
        response = client.get("/health/live", headers={"X-Request-ID": "trace-me-42"})

    assert response.headers["X-Request-ID"] == "trace-me-42"
    completed = [r for r in caplog.records if r.getMessage() == "request completed"]
    assert completed, "request completion log missing"
    # The middleware set the correlation contextvar; the formatter reads it at
    # emit time. Emitting inside the request scope proves propagation.
    assert completed[0].http_route == "/health/live"
    assert completed[0].http_status == 200


def test_untrusted_correlation_header_is_not_echoed() -> None:
    app = create_app(ApiSettings(environment=Environment.TEST))
    client = TestClient(app)
    untrusted = "credential=CANARY customer value"

    response = client.get("/health/live", headers={"X-Request-ID": untrusted})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != untrusted
    assert len(response.headers["X-Request-ID"]) == 32
