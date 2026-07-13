"""The memory reference adapter must pass the full contract (STO-001),
plus memory-specific signed-URL verification the contract can't demand of
real S3 (where the vendor validates signatures)."""

from datetime import UTC, datetime, timedelta

import pytest

from soa_storage import MemoryObjectStore, ObjectStore, SignedUrlExpiredError
from soa_storage.contract import ObjectStoreContract


class TestMemoryObjectStoreContract(ObjectStoreContract):
    async def make_store(self) -> ObjectStore:
        return MemoryObjectStore()


async def test_signed_url_verifies_and_expires() -> None:
    store = MemoryObjectStore()
    await store.put("org-1/doc-9/original.pdf", b"pdf bytes")
    signed = await store.signed_download_url("org-1/doc-9/original.pdf", expires_in_seconds=60)

    assert store.verify_signed_url(signed.url) == "org-1/doc-9/original.pdf"
    with pytest.raises(SignedUrlExpiredError):
        store.verify_signed_url(signed.url, now=datetime.now(tz=UTC) + timedelta(seconds=61))


async def test_tampered_signed_url_is_rejected() -> None:
    store = MemoryObjectStore()
    await store.put("org-1/doc-9/original.pdf", b"pdf bytes")
    await store.put("org-2/doc-1/original.pdf", b"other tenant")
    signed = await store.signed_download_url("org-1/doc-9/original.pdf", expires_in_seconds=60)

    # Swapping the key (e.g. to another tenant's object) breaks the signature.
    tampered = signed.url.replace("org-1/doc-9", "org-2/doc-1")
    with pytest.raises(ValueError, match="failed verification"):
        store.verify_signed_url(tampered)


async def test_urls_from_a_different_store_instance_fail() -> None:
    issuing = MemoryObjectStore()
    other = MemoryObjectStore()
    await issuing.put("k", b"v")
    signed = await issuing.signed_download_url("k", expires_in_seconds=60)
    with pytest.raises(ValueError, match="failed verification"):
        other.verify_signed_url(signed.url)


def test_protocol_conformance_is_structural() -> None:
    # Domain code type-checks against the Protocol, never the adapter.
    assert isinstance(MemoryObjectStore(), ObjectStore)


def test_no_vendor_sdk_types_leak_from_the_interface() -> None:
    # STO-001 acceptance: importing the package (interface + memory
    # adapter) must not pull in any vendor SDK. Checked in a subprocess so
    # other test modules (e.g. the S3 adapter's own tests) can't pollute
    # sys.modules first.
    import subprocess
    import sys

    probe = (
        "import sys, soa_storage;"
        "prefixes = ('boto', 'aiobotocore', 'minio', 'google.cloud', 'azure');"
        "leaked = [m for m in sys.modules if m.startswith(prefixes)];"
        "assert leaked == [], f'vendor SDK modules imported: {leaked}'"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)
