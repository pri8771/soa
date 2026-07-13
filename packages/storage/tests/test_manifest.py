"""Reconciliation report tests (STO-005): missing, extra, mismatched —
and never any object content in the report."""

import json

from soa_storage import MemoryObjectStore, sha256_hex
from soa_storage.manifest import reconcile


async def test_clean_store_reconciles_clean() -> None:
    store = MemoryObjectStore()
    await store.put("orgs/a/doc/original/x", b"alpha")
    await store.put("orgs/a/doc/ocr/y", b"beta")
    expected = {
        "orgs/a/doc/original/x": sha256_hex(b"alpha"),
        "orgs/a/doc/ocr/y": sha256_hex(b"beta"),
    }
    report = await reconcile(store, expected, prefix="orgs/")
    assert report.clean
    assert report.verified == 2
    assert report.to_dict()["clean"] is True


async def test_missing_extra_and_corrupted_objects_are_reported() -> None:
    store = MemoryObjectStore()
    await store.put("orgs/a/doc/original/kept", b"good bytes")
    # Corruption: the store holds different bytes than the record claims.
    await store.put("orgs/a/doc/original/corrupt", b"TAMPERED CONTENT")
    # Orphan: exists in the store, unknown to the manifest.
    await store.put("orgs/a/doc/original/orphan", b"who wrote this")

    expected = {
        "orgs/a/doc/original/kept": sha256_hex(b"good bytes"),
        "orgs/a/doc/original/corrupt": sha256_hex(b"original content"),
        "orgs/a/doc/original/lost": sha256_hex(b"never made it"),
    }
    report = await reconcile(store, expected, prefix="orgs/")

    assert not report.clean
    assert report.verified == 1
    assert report.missing == ["orgs/a/doc/original/lost"]
    assert report.extra == ["orgs/a/doc/original/orphan"]
    (mismatch,) = report.mismatched
    assert mismatch.key == "orgs/a/doc/original/corrupt"
    assert mismatch.expected_sha256 == sha256_hex(b"original content")
    assert mismatch.actual_sha256 == sha256_hex(b"TAMPERED CONTENT")

    # Acceptance: the report exposes keys and hashes, never content.
    serialized = json.dumps(report.to_dict())
    assert "TAMPERED CONTENT" not in serialized
    assert "good bytes" not in serialized


async def test_prefix_scopes_extra_detection() -> None:
    store = MemoryObjectStore()
    await store.put("orgs/a/doc/original/x", b"alpha")
    await store.put("unrelated/backup.tar", b"not ours")
    expected = {"orgs/a/doc/original/x": sha256_hex(b"alpha")}
    report = await reconcile(store, expected, prefix="orgs/")
    assert report.clean, "objects outside the artifact namespace are not 'extra'"
