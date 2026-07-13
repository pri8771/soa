"""Provider capability registry (AIO-001).

Every document-AI provider adapter — native text, OCR/layout,
classifier, splitter, extraction — registers here with metadata the
platform selects on: what it can do, which languages it covers, and its
data policy (where content is processed, whether it leaves the
deployment, whether the vendor retains it or trains on it).

Selection FAILS CLOSED in both directions:

- resolving an unregistered provider raises, it never falls back;
- ``select_providers`` defaults deny third-party processing, content
  retention, and training — a hosted adapter is only eligible when the
  caller (the resolved tenant policy) explicitly allows each of those.

Capability slugs deliberately match the strings CFG-005 provider
policies use (``ocr``, ``field_extraction``): a policy that names a
capability names the same thing this registry keys on.

Adapters may also be UNregistered (AIO-006: unavailable credentials
disable a capability clearly) — after that, resolution fails with the
same closed error instead of limping along.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Wildcard language entry: the provider handles any language. Must be
#: the ONLY entry when used — "any plus specifics" is a contradiction.
ANY_LANGUAGE = "*"


class Capability(StrEnum):
    NATIVE_TEXT = "native_text"
    OCR = "ocr"
    CLASSIFY = "classify"
    SPLIT = "split"
    FIELD_EXTRACTION = "field_extraction"


@dataclass(frozen=True)
class DataPolicy:
    """Where content goes when this provider processes it. These are the
    facts tenant retention/provider policies are enforced against, so an
    adapter must declare them honestly — optimistic metadata here is a
    compliance bug, not a marketing choice."""

    #: Where processing happens: ``local`` (inside the deployment) or a
    #: lowercase region slug for hosted providers (``eu``, ``us``, ...).
    processing_region: str
    sends_content_to_third_party: bool
    retains_content: bool
    uses_content_for_training: bool

    def __post_init__(self) -> None:
        region = self.processing_region
        if not region or region != region.strip().lower():
            raise ValueError("processing_region must be a non-empty lowercase slug")
        if region == "local" and self.sends_content_to_third_party:
            raise ValueError("local processing cannot also send content to a third party")


#: The policy of everything that runs inside the deployment (the mock,
#: native-text, local OCR): content never leaves, nothing is retained.
LOCAL_DATA_POLICY = DataPolicy(
    processing_region="local",
    sends_content_to_third_party=False,
    retains_content=False,
    uses_content_for_training=False,
)


@dataclass(frozen=True)
class ProviderInfo:
    """Registration metadata for one adapter. ``name`` must equal the
    ``name`` the provider instance reports on its results — the registry
    verifies that at construction time."""

    name: str
    capability: Capability
    #: Lowercase language tags the provider covers, or ``(ANY_LANGUAGE,)``.
    languages: tuple[str, ...]
    data_policy: DataPolicy

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider name must be non-empty")
        if not self.languages:
            raise ValueError("a provider must declare at least one language (or ANY_LANGUAGE)")
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("duplicate language declarations")
        if ANY_LANGUAGE in self.languages and len(self.languages) > 1:
            raise ValueError("ANY_LANGUAGE must be the only language entry")
        for tag in self.languages:
            if tag != ANY_LANGUAGE and (not tag or tag != tag.strip().lower()):
                raise ValueError(f"language tag {tag!r} must be a non-empty lowercase tag")

    def supports_language(self, language: str) -> bool:
        return ANY_LANGUAGE in self.languages or language.lower() in self.languages


ProviderFactory = Callable[[], Any]


class UnknownProviderError(Exception):
    def __init__(self, capability: Capability, name: str, known: tuple[str, ...]) -> None:
        super().__init__(
            f"no {capability.value} provider named {name!r} is registered — "
            f"registered: {', '.join(sorted(known)) or '(none)'}"
        )


class NoCapableProviderError(Exception):
    """No registered provider satisfies the constraints. The message
    names the constraint that emptied the pool so operators fix the real
    gap instead of guessing."""


@dataclass(frozen=True)
class _Registration:
    info: ProviderInfo
    factory: ProviderFactory


_REGISTRY: dict[tuple[Capability, str], _Registration] = {}


def register_provider(info: ProviderInfo, factory: ProviderFactory) -> None:
    key = (info.capability, info.name)
    if key in _REGISTRY:
        raise ValueError(f"{info.capability.value} provider {info.name!r} is already registered")
    _REGISTRY[key] = _Registration(info=info, factory=factory)


def unregister_provider(capability: Capability, name: str) -> None:
    """Disable a capability cleanly (e.g. hosted credentials missing at
    startup). Resolution afterwards fails closed with the usual error."""
    key = (capability, name)
    if key not in _REGISTRY:
        raise UnknownProviderError(capability, name, _names_for(capability))
    del _REGISTRY[key]


def _names_for(capability: Capability) -> tuple[str, ...]:
    return tuple(name for cap, name in _REGISTRY if cap == capability)


def provider_info(capability: Capability, name: str) -> ProviderInfo:
    registration = _REGISTRY.get((capability, name))
    if registration is None:
        raise UnknownProviderError(capability, name, _names_for(capability))
    return registration.info


def create_provider(capability: Capability, name: str) -> Any:
    """Build the provider instance. The instance must report the
    registered name — a mismatch is a wiring bug and fails loudly."""
    registration = _REGISTRY.get((capability, name))
    if registration is None:
        raise UnknownProviderError(capability, name, _names_for(capability))
    instance = registration.factory()
    reported = getattr(instance, "name", None)
    if reported != name:
        raise ValueError(
            f"provider registered as {name!r} reports name {reported!r} — "
            "results would be attributed to the wrong provider"
        )
    return instance


def registered_providers(capability: Capability | None = None) -> tuple[ProviderInfo, ...]:
    infos = [
        registration.info
        for (cap, _), registration in sorted(_REGISTRY.items())
        if capability is None or cap == capability
    ]
    return tuple(infos)


def select_providers(
    capability: Capability,
    *,
    language: str | None = None,
    region: str | None = None,
    allow_third_party_processing: bool = False,
    allow_content_retention: bool = False,
    allow_training_on_content: bool = False,
) -> tuple[ProviderInfo, ...]:
    """Providers eligible under the given constraints, sorted by name.

    Data-policy defaults are the STRICT ones — a caller must explicitly
    allow third-party processing, retention, or training for a provider
    that does any of them to be eligible.
    """
    pool = [info for info in registered_providers(capability)]
    if language is not None:
        pool = [info for info in pool if info.supports_language(language)]
    if region is not None:
        pool = [info for info in pool if info.data_policy.processing_region == region.lower()]
    if not allow_third_party_processing:
        pool = [info for info in pool if not info.data_policy.sends_content_to_third_party]
    if not allow_content_retention:
        pool = [info for info in pool if not info.data_policy.retains_content]
    if not allow_training_on_content:
        pool = [info for info in pool if not info.data_policy.uses_content_for_training]
    return tuple(sorted(pool, key=lambda info: info.name))


def require_provider(
    capability: Capability,
    *,
    language: str | None = None,
    region: str | None = None,
    allow_third_party_processing: bool = False,
    allow_content_retention: bool = False,
    allow_training_on_content: bool = False,
) -> ProviderInfo:
    """The first eligible provider, or a closed error naming the
    constraint that eliminated every candidate."""
    registered = registered_providers(capability)
    if not registered:
        raise NoCapableProviderError(f"no {capability.value} provider is registered at all")

    steps: list[tuple[str, dict[str, Any]]] = []
    if language is not None:
        steps.append((f"language {language!r}", {"language": language}))
    if region is not None:
        steps.append((f"region {region!r}", {"region": region}))
    steps.append(
        (
            "data policy (third-party processing / retention / training not allowed)",
            {
                "allow_third_party_processing": allow_third_party_processing,
                "allow_content_retention": allow_content_retention,
                "allow_training_on_content": allow_training_on_content,
            },
        )
    )

    constraints: dict[str, Any] = {
        # Start permissive, tighten one step at a time to find the culprit.
        "allow_third_party_processing": True,
        "allow_content_retention": True,
        "allow_training_on_content": True,
    }
    for label, extra in steps:
        constraints.update(extra)
        pool = select_providers(capability, **constraints)
        if not pool:
            raise NoCapableProviderError(
                f"no {capability.value} provider satisfies the {label} constraint — "
                f"registered: {', '.join(info.name for info in registered)}"
            )
    return select_providers(capability, **constraints)[0]


__all__ = [
    "ANY_LANGUAGE",
    "LOCAL_DATA_POLICY",
    "Capability",
    "DataPolicy",
    "NoCapableProviderError",
    "ProviderFactory",
    "ProviderInfo",
    "UnknownProviderError",
    "create_provider",
    "provider_info",
    "register_provider",
    "registered_providers",
    "require_provider",
    "select_providers",
    "unregister_provider",
]
