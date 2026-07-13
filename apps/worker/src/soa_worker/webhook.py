"""Signed webhook delivery adapter (EXP-007).

Delivers an export payload to a tenant-configured HTTPS endpoint with:

- a TIMESTAMPED HMAC SIGNATURE (``X-SOA-Signature: t=<unix>,v1=<hex>``
  over ``{t}.{body}``) that receivers verify with the shared secret —
  ``verify_webhook_signature`` is shipped here so the receiver contract
  is executable, including the replay window;
- an IDEMPOTENCY HEADER carrying the export's business key: retries
  send the SAME key, so receivers deduplicate no matter how many
  attempts it takes;
- a bounded TIMEOUT and honest RESPONSE CLASSIFICATION (2xx delivered;
  408/429/5xx and network failures retryable; other 4xx terminal);
- REDACTED STORAGE: what comes back is reduced to a status code and a
  short excerpt with credential-looking material stripped — raw
  responses are never persisted;
- SSRF PROTECTION and a DESTINATION ALLOWLIST: HTTPS only, the host
  must match the tenant's allowlist (fail closed when none is
  configured), and every resolved address must be public — loopback,
  private, link-local, and reserved ranges are refused before any
  connection is attempted.
"""

import hashlib
import hmac
import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

SIGNATURE_HEADER = "X-SOA-Signature"
IDEMPOTENCY_HEADER = "X-SOA-Idempotency-Key"
ATTEMPT_HEADER = "X-SOA-Delivery-Attempt"

#: Receivers must see a timestamp within this window (seconds), which
#: bounds replay of a captured request.
REPLAY_TOLERANCE_SECONDS = 300

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Credential-looking material never lands in storage.
_REDACT = re.compile(
    r"(?i)(bearer\s+[a-z0-9._-]+|authorization[^\s,;]*|api[_-]?key[^\s,;]*|secret[^\s,;]*)"
)
_EXCERPT_LENGTH = 200


# -- signature ---------------------------------------------------------------------


def sign_webhook(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_webhook_signature(
    secret: str,
    body: bytes,
    header: str,
    *,
    now: int,
    tolerance_seconds: int = REPLAY_TOLERANCE_SECONDS,
) -> bool:
    """The receiver-side check, shipped with the sender so the contract
    is executable: parse ``t=...,v1=...``, refuse timestamps outside the
    replay window, and compare constant-time."""
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    timestamp_raw = parts.get("t")
    provided = parts.get("v1")
    if timestamp_raw is None or provided is None or not timestamp_raw.isdigit():
        return False
    timestamp = int(timestamp_raw)
    if abs(now - timestamp) > tolerance_seconds:
        return False
    expected = sign_webhook(secret, body, timestamp).split("v1=", 1)[1]
    return hmac.compare_digest(expected, provided)


# -- destination policy (SSRF + allowlist) -------------------------------------------


class DestinationRefusedError(Exception):
    pass


def _host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    for entry in allowlist:
        if entry.startswith("*."):
            if host == entry[2:] or host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def validate_destination(
    url: str,
    *,
    allowlist: Sequence[str],
    resolve: Callable[[str], list[str]] = _default_resolver,
) -> None:
    """Refuse anything that is not an explicitly allowlisted PUBLIC HTTPS
    destination. Fails closed: no allowlist means no deliveries."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise DestinationRefusedError(f"webhook destinations must be https:// — got {url!r}")
    host = parsed.hostname
    if not host:
        raise DestinationRefusedError(f"{url!r} has no hostname")
    if not allowlist:
        raise DestinationRefusedError(
            "no destination allowlist is configured for this integration — "
            "deliveries fail closed until one is set"
        )
    if not _host_allowed(host, allowlist):
        raise DestinationRefusedError(f"host {host!r} is not on the destination allowlist")
    try:
        addresses = resolve(host)
    except OSError as error:
        raise DestinationRefusedError(f"cannot resolve {host!r}: {error}") from None
    if not addresses:
        raise DestinationRefusedError(f"{host!r} resolved to no addresses")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global or address.is_multicast:
            raise DestinationRefusedError(
                f"{host!r} resolves to non-public address {raw} — refused (SSRF protection)"
            )


# -- delivery -----------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookResult:
    outcome: str  # delivered | retryable_error | terminal_error
    response_status: int | None
    safe_error: str | None
    #: Status + redacted excerpt; raw responses are never stored.
    redacted_response: str | None


def redact_response_excerpt(text: str) -> str:
    return _REDACT.sub("[redacted]", text)[:_EXCERPT_LENGTH]


def classify_response(status: int) -> str:
    if 200 <= status < 300:
        return "delivered"
    if status in (408, 429) or status >= 500:
        return "retryable_error"
    return "terminal_error"


async def deliver_webhook(
    client: httpx.AsyncClient,
    *,
    url: str,
    body: bytes,
    secret: str,
    business_key: str,
    attempt_number: int,
    timestamp: int,
    allowlist: Sequence[str],
    resolve: Callable[[str], list[str]] = _default_resolver,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> WebhookResult:
    """One delivery attempt. The business key rides the idempotency
    header on EVERY attempt — a retry is the same business event."""
    validate_destination(url, allowlist=allowlist, resolve=resolve)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_webhook(secret, body, timestamp),
        IDEMPOTENCY_HEADER: business_key,
        ATTEMPT_HEADER: str(attempt_number),
    }
    try:
        response = await client.post(url, content=body, headers=headers, timeout=timeout_seconds)
    except httpx.TimeoutException:
        return WebhookResult(
            outcome="retryable_error",
            response_status=None,
            safe_error=f"receiver did not respond within {timeout_seconds:g}s",
            redacted_response=None,
        )
    except httpx.HTTPError as error:
        return WebhookResult(
            outcome="retryable_error",
            response_status=None,
            safe_error=f"network error: {type(error).__name__}",
            redacted_response=None,
        )
    outcome = classify_response(response.status_code)
    excerpt = redact_response_excerpt(response.text) if response.text else ""
    return WebhookResult(
        outcome=outcome,
        response_status=response.status_code,
        safe_error=(
            None if outcome == "delivered" else f"receiver returned {response.status_code}"
        ),
        redacted_response=f"{response.status_code}: {excerpt}"[: _EXCERPT_LENGTH + 10],
    )
