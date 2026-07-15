"""Secret-store interface and local adapters (SEC-005).

The platform holds tenant-provided secrets (webhook signing keys today;
provider API keys when hosted adapters land). The database stores only
REFERENCES — opaque ``secretref://<provider>/<name>`` strings — while
the values live in a secret store behind this interface:

- :class:`MemorySecretStore` — tests and throwaway development runs;
- :class:`FileSecretStore` — persistent local development (0600 files
  under a 0700 directory, never inside the repository);
- :class:`EnvSecretStore` — read-only resolution of platform-level
  secrets injected through the environment;
- ``soa_storage.AwsSecretsManagerStore`` — the production adapter
  (lives in soa-storage, which owns the AWS client dependency).

Rules the interface enforces everywhere:

- a reference is NEVER the value: parsing validates shape, and a store
  refuses references minted for a different provider — a mixed-up
  configuration fails loudly instead of resolving the wrong secret;
- rotation is put-new-then-revoke-old: ``put`` always creates a new
  name, so a reference is immutable once issued and audit trails can
  name exactly which credential version signed what;
- ``resolve`` of a revoked or missing secret raises
  :class:`SecretNotFoundError` — callers decide what absence means,
  the store never silently substitutes.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "EnvSecretStore",
    "FileSecretStore",
    "MemorySecretStore",
    "SecretNotFoundError",
    "SecretReference",
    "SecretStore",
    "SecretStoreError",
    "SecretStoreReadOnlyError",
    "SecretStoreUnavailableError",
]

_REFERENCE_SCHEME = "secretref://"
#: Names are path-like for namespacing (``orgs/<id>/integrations/...``)
#: but strictly bounded: no traversal, no absolute paths, no blanks.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_MAX_NAME_LENGTH = 512


class SecretStoreError(Exception):
    """Base for secret-store failures."""


class SecretNotFoundError(SecretStoreError):
    """The reference is valid but no live value exists for it."""


class SecretStoreReadOnlyError(SecretStoreError):
    """This store cannot create or revoke secrets."""


class SecretStoreUnavailableError(SecretStoreError):
    """The configured store could not be reached; retry may succeed."""


@dataclass(frozen=True)
class SecretReference:
    """An opaque pointer to a secret value — safe to persist and log."""

    provider: str
    name: str

    def __post_init__(self) -> None:
        if not _PROVIDER_PATTERN.match(self.provider):
            raise ValueError(f"invalid secret provider {self.provider!r}")
        if len(self.name) > _MAX_NAME_LENGTH or not _NAME_PATTERN.match(self.name):
            raise ValueError("invalid secret name — bounded path-like names only")

    def __str__(self) -> str:
        return f"{_REFERENCE_SCHEME}{self.provider}/{self.name}"

    @classmethod
    def parse(cls, raw: str) -> "SecretReference":
        if not raw.startswith(_REFERENCE_SCHEME):
            raise ValueError("not a secret reference — expected secretref://<provider>/<name>")
        provider, _, name = raw.removeprefix(_REFERENCE_SCHEME).partition("/")
        return cls(provider=provider, name=name)


@runtime_checkable
class SecretStore(Protocol):
    """The contract every adapter implements. ``put`` returns the
    reference for a NEW name — values are immutable per reference and
    rotation issues a fresh one."""

    @property
    def provider(self) -> str: ...

    async def put(self, name: str, value: str) -> SecretReference: ...

    async def resolve(self, reference: SecretReference) -> str: ...

    async def revoke(self, reference: SecretReference) -> None: ...


def _require_same_provider(store_provider: str, reference: SecretReference) -> None:
    if reference.provider != store_provider:
        raise SecretStoreError(
            f"reference {reference} belongs to provider {reference.provider!r}; "
            f"this store is {store_provider!r} — refusing to resolve across providers"
        )


@dataclass
class MemorySecretStore:
    """Test/ephemeral-development store. Nothing survives the process."""

    _values: dict[str, str] = field(default_factory=dict)

    @property
    def provider(self) -> str:
        return "memory"

    async def put(self, name: str, value: str) -> SecretReference:
        reference = SecretReference(provider=self.provider, name=name)
        if name in self._values:
            raise SecretStoreError(f"secret {name!r} already exists — rotation uses a new name")
        self._values[name] = value
        return reference

    async def resolve(self, reference: SecretReference) -> str:
        _require_same_provider(self.provider, reference)
        try:
            return self._values[reference.name]
        except KeyError:
            raise SecretNotFoundError(f"no live secret behind {reference}") from None

    async def revoke(self, reference: SecretReference) -> None:
        _require_same_provider(self.provider, reference)
        self._values.pop(reference.name, None)  # idempotent


@dataclass
class EnvSecretStore:
    """Read-only resolution of platform-level secrets from environment
    variables (``secretref://env/SOME_VAR``). Tenant secrets never go
    here — there is no write path at all."""

    @property
    def provider(self) -> str:
        return "env"

    async def put(self, name: str, value: str) -> SecretReference:
        raise SecretStoreReadOnlyError(
            "the env store is read-only — tenant secrets need a writable backend"
        )

    async def resolve(self, reference: SecretReference) -> str:
        _require_same_provider(self.provider, reference)
        value = os.environ.get(reference.name)
        if value is None or value == "":
            raise SecretNotFoundError(f"environment variable {reference.name} is not set")
        return value

    async def revoke(self, reference: SecretReference) -> None:
        raise SecretStoreReadOnlyError("the env store is read-only")


class FileSecretStore:
    """Local-development store: one 0600 file per secret under a 0700
    directory. Real persistence semantics without a cloud dependency —
    and settings validation keeps it out of production."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory).expanduser()
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    @property
    def provider(self) -> str:
        return "file"

    def _path(self, name: str) -> Path:
        # The reference constructor already refused traversal shapes;
        # resolve() double-checks containment anyway.
        path = (self._directory / name).resolve()
        if not path.is_relative_to(self._directory.resolve()):
            raise SecretStoreError(f"secret name {name!r} escapes the store directory")
        return path

    async def put(self, name: str, value: str) -> SecretReference:
        reference = SecretReference(provider=self.provider, name=name)
        path = self._path(name)
        if path.exists():
            raise SecretStoreError(f"secret {name!r} already exists — rotation uses a new name")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)
        return reference

    async def resolve(self, reference: SecretReference) -> str:
        _require_same_provider(self.provider, reference)
        path = self._path(reference.name)
        try:
            return path.read_text()
        except FileNotFoundError:
            raise SecretNotFoundError(f"no live secret behind {reference}") from None

    async def revoke(self, reference: SecretReference) -> None:
        _require_same_provider(self.provider, reference)
        self._path(reference.name).unlink(missing_ok=True)  # idempotent
