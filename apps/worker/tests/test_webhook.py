"""Signed webhook adapter tests (EXP-007): signature + replay window,
idempotency across retries, timeout, response classification, redacted
storage, and SSRF/allowlist enforcement."""

import httpx
import pytest

from soa_worker.webhook import (
    ATTEMPT_HEADER,
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    DestinationRefusedError,
    classify_response,
    deliver_webhook,
    sign_webhook,
    validate_destination,
    verify_webhook_signature,
)

SECRET = "whsec_test_secret"
BODY = b'{"PoNumber":"PO-100042"}'
NOW = 1_800_000_000
ALLOWLIST = ["erp.northstar.example", "*.hooks.example"]
PUBLIC = ["93.184.216.34"]


def public_resolver(_host: str) -> list[str]:
    return PUBLIC


# -- signature ---------------------------------------------------------------------


def test_receiver_can_verify_the_signature() -> None:
    header = sign_webhook(SECRET, BODY, NOW)
    assert header.startswith(f"t={NOW},v1=")
    assert verify_webhook_signature(SECRET, BODY, header, now=NOW) is True
    # Tampered body, wrong secret, malformed header: all refused.
    assert verify_webhook_signature(SECRET, BODY + b"x", header, now=NOW) is False
    assert verify_webhook_signature("other-secret", BODY, header, now=NOW) is False
    assert verify_webhook_signature(SECRET, BODY, "garbage", now=NOW) is False


def test_replay_window_bounds_old_signatures() -> None:
    header = sign_webhook(SECRET, BODY, NOW)
    assert verify_webhook_signature(SECRET, BODY, header, now=NOW + 299) is True
    assert verify_webhook_signature(SECRET, BODY, header, now=NOW + 301) is False
    assert verify_webhook_signature(SECRET, BODY, header, now=NOW - 301) is False


# -- destination policy ---------------------------------------------------------------


def test_destinations_fail_closed_without_an_allowlist() -> None:
    with pytest.raises(DestinationRefusedError, match="fail closed"):
        validate_destination(
            "https://erp.northstar.example/orders", allowlist=[], resolve=public_resolver
        )


def test_non_allowlisted_and_non_https_destinations_are_refused() -> None:
    with pytest.raises(DestinationRefusedError, match="not on the destination allowlist"):
        validate_destination(
            "https://evil.example/orders", allowlist=ALLOWLIST, resolve=public_resolver
        )
    with pytest.raises(DestinationRefusedError, match="must be https"):
        validate_destination(
            "http://erp.northstar.example/orders", allowlist=ALLOWLIST, resolve=public_resolver
        )
    # Wildcard entries admit subdomains only under the suffix.
    validate_destination("https://a.hooks.example/x", allowlist=ALLOWLIST, resolve=public_resolver)
    with pytest.raises(DestinationRefusedError):
        validate_destination(
            "https://hooksXexample/x", allowlist=ALLOWLIST, resolve=public_resolver
        )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.7", "172.16.4.4", "192.168.1.1", "169.254.169.254", "::1", "fc00::1"],
)
def test_private_and_loopback_addresses_are_blocked(address: str) -> None:
    with pytest.raises(DestinationRefusedError, match="SSRF"):
        validate_destination(
            "https://erp.northstar.example/orders",
            allowlist=ALLOWLIST,
            resolve=lambda _host: [address],
        )


# -- delivery -----------------------------------------------------------------------


def make_client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_delivery_sends_verifiable_signature_and_stable_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    async with make_client(handler) as client:
        for attempt in (1, 2):  # a retry is the SAME business event
            result = await deliver_webhook(
                client,
                url="https://erp.northstar.example/orders",
                body=BODY,
                secret=SECRET,
                business_key="export:i:d:r",
                attempt_number=attempt,
                timestamp=NOW,
                allowlist=ALLOWLIST,
                resolve=public_resolver,
            )
            assert result.outcome == "delivered"
            assert result.response_status == 200
    first, second = seen
    # The receiver can verify what actually arrived.
    assert verify_webhook_signature(SECRET, first.content, first.headers[SIGNATURE_HEADER], now=NOW)
    # Retries carry the SAME idempotency key, and say which attempt.
    assert first.headers[IDEMPOTENCY_HEADER] == second.headers[IDEMPOTENCY_HEADER]
    assert (first.headers[ATTEMPT_HEADER], second.headers[ATTEMPT_HEADER]) == ("1", "2")


async def test_timeout_is_a_retryable_outcome() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    async with make_client(handler) as client:
        result = await deliver_webhook(
            client,
            url="https://erp.northstar.example/orders",
            body=BODY,
            secret=SECRET,
            business_key="k",
            attempt_number=1,
            timestamp=NOW,
            allowlist=ALLOWLIST,
            resolve=public_resolver,
        )
    assert result.outcome == "retryable_error"
    assert result.safe_error is not None and "did not respond" in result.safe_error


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, "delivered"),
        (201, "delivered"),
        (408, "retryable_error"),
        (429, "retryable_error"),
        (500, "retryable_error"),
        (503, "retryable_error"),
        (400, "terminal_error"),
        (404, "terminal_error"),
        (422, "terminal_error"),
    ],
)
def test_response_classification(status: int, expected: str) -> None:
    assert classify_response(status) == expected


async def test_responses_are_stored_redacted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text="rejected; hint: use Authorization: Bearer sk-live-topsecret and api_key=abc "
            + "x" * 500,
        )

    async with make_client(handler) as client:
        result = await deliver_webhook(
            client,
            url="https://erp.northstar.example/orders",
            body=BODY,
            secret=SECRET,
            business_key="k",
            attempt_number=1,
            timestamp=NOW,
            allowlist=ALLOWLIST,
            resolve=public_resolver,
        )
    assert result.outcome == "terminal_error"
    assert result.redacted_response is not None
    assert "sk-live-topsecret" not in result.redacted_response
    assert "api_key=abc" not in result.redacted_response
    assert len(result.redacted_response) <= 210  # bounded excerpt
