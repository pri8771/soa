"""Exact-host allowlist and SSRF protection for every outbound request."""

import ipaddress
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlparse


class DestinationRefusedError(Exception):
    """The destination is outside the explicitly configured public hosts."""


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def validate_destination(
    url: str,
    *,
    allowlist: Sequence[str],
    resolve: Callable[[str], list[str]] | None = None,
) -> None:
    """Require HTTPS, an exact hostname match, and only public DNS answers.

    Wildcards are deliberately unsupported. A suffix allowlist is too easy
    to widen accidentally, and a DNS answer containing even one private,
    loopback, link-local, reserved, or multicast address fails closed.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise DestinationRefusedError("outbound destinations must be https://")
    if parsed.username is not None or parsed.password is not None:
        raise DestinationRefusedError("outbound destination URLs cannot contain user info")
    try:
        port = parsed.port
    except ValueError:
        raise DestinationRefusedError("outbound destination has an invalid port") from None
    if port not in (None, 443):
        raise DestinationRefusedError("outbound https destinations must use port 443")
    host = parsed.hostname
    if not host:
        raise DestinationRefusedError("outbound destination has no hostname")
    normalized_allowlist = {
        entry.strip().lower().rstrip(".") for entry in allowlist if entry.strip()
    }
    if not normalized_allowlist:
        raise DestinationRefusedError(
            "no outbound destination allowlist is configured; outbound access must fail closed"
        )
    if any("*" in entry for entry in normalized_allowlist):
        raise DestinationRefusedError("destination allowlist entries must be exact hostnames")
    normalized_host = host.lower().rstrip(".")
    if normalized_host not in normalized_allowlist:
        raise DestinationRefusedError(f"host {normalized_host!r} is not allowlisted")
    resolver = resolve or _default_resolver
    try:
        addresses = resolver(normalized_host)
    except OSError as error:
        raise DestinationRefusedError(f"cannot resolve {normalized_host!r}: {error}") from None
    if not addresses:
        raise DestinationRefusedError(f"{normalized_host!r} resolved to no addresses")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise DestinationRefusedError(
                f"{normalized_host!r} resolved to an invalid address"
            ) from None
        if not address.is_global or address.is_multicast:
            raise DestinationRefusedError(
                f"{normalized_host!r} resolves to a non-public address; refused by SSRF protection"
            )
