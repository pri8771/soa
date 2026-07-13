"""Manifest reconciliation (STO-005).

Compares an expected manifest (object key → SHA-256, exported from the
artifact records) against what the store actually holds. The report
names keys and hashes ONLY — never object content — so it is safe to
ship to an operator console, a backup log, or a support ticket.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from soa_storage.store import ObjectStore


@dataclass(frozen=True)
class HashMismatch:
    key: str
    expected_sha256: str
    actual_sha256: str


@dataclass(frozen=True)
class ReconciliationReport:
    verified: int = 0
    #: In the manifest, absent from the store — lost or never written.
    missing: list[str] = field(default_factory=list)
    #: In the store, absent from the manifest — orphaned or foreign.
    extra: list[str] = field(default_factory=list)
    #: Present in both, but the stored checksum disagrees.
    mismatched: list[HashMismatch] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.extra or self.mismatched)

    def to_dict(self) -> dict[str, object]:
        return {
            "verified": self.verified,
            "missing": list(self.missing),
            "extra": list(self.extra),
            "mismatched": [
                {
                    "key": m.key,
                    "expected_sha256": m.expected_sha256,
                    "actual_sha256": m.actual_sha256,
                }
                for m in self.mismatched
            ],
            "clean": self.clean,
        }


async def reconcile(
    store: ObjectStore, expected: Mapping[str, str], *, prefix: str = ""
) -> ReconciliationReport:
    """Verify every manifest entry against the store and find objects the
    manifest doesn't know about (under ``prefix``). Uses metadata only —
    no object content is fetched."""
    present = await store.list_by_manifest(sorted(expected))
    missing: list[str] = []
    mismatched: list[HashMismatch] = []
    verified = 0
    for key, expected_sha in sorted(expected.items()):
        metadata = present.get(key)
        if metadata is None:
            missing.append(key)
        elif metadata.sha256 != expected_sha:
            mismatched.append(
                HashMismatch(key=key, expected_sha256=expected_sha, actual_sha256=metadata.sha256)
            )
        else:
            verified += 1

    stored_keys = await store.list_keys(prefix)
    extra = [key for key in stored_keys if key not in expected]

    return ReconciliationReport(
        verified=verified, missing=missing, extra=extra, mismatched=mismatched
    )
