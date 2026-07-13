"""Reusable ObjectStore contract suite (STO-001).

Every adapter's test module subclasses ``ObjectStoreContract`` and
implements ``make_store``. The suite encodes the behaviours domain code
relies on; an adapter that cannot pass it is not an ObjectStore, however
S3-compatible its vendor claims to be.
"""

from datetime import UTC, datetime

import pytest

from soa_storage.store import ChecksumMismatchError, ObjectNotFoundError, ObjectStore, sha256_hex


class ObjectStoreContract:
    """Subclass per adapter and override make_store()."""

    async def make_store(self) -> ObjectStore:
        raise NotImplementedError

    async def test_put_get_roundtrip_records_checksum(self) -> None:
        store = await self.make_store()
        data = b"purchase order 42"
        metadata = await store.put("org-1/doc-1/original.pdf", data, content_type="application/pdf")
        assert metadata.size == len(data)
        assert metadata.sha256 == sha256_hex(data)
        assert metadata.content_type == "application/pdf"
        assert await store.get("org-1/doc-1/original.pdf") == data

    async def test_put_refuses_mismatched_declared_checksum(self) -> None:
        store = await self.make_store()
        with pytest.raises(ChecksumMismatchError):
            await store.put("org-1/doc-1/original.pdf", b"real bytes", sha256=sha256_hex(b"other"))
        # The refused write must not create the object.
        with pytest.raises(ObjectNotFoundError):
            await store.head("org-1/doc-1/original.pdf")

    async def test_head_returns_metadata_without_content(self) -> None:
        store = await self.make_store()
        await store.put("k", b"abc", content_type="text/plain")
        metadata = await store.head("k")
        assert (metadata.size, metadata.content_type) == (3, "text/plain")

    async def test_missing_objects_raise_not_found(self) -> None:
        store = await self.make_store()
        with pytest.raises(ObjectNotFoundError):
            await store.get("nope")
        with pytest.raises(ObjectNotFoundError):
            await store.head("nope")
        with pytest.raises(ObjectNotFoundError):
            await store.delete("nope")

    async def test_delete_removes_the_object(self) -> None:
        store = await self.make_store()
        await store.put("k", b"abc")
        await store.delete("k")
        with pytest.raises(ObjectNotFoundError):
            await store.get("k")

    async def test_copy_preserves_bytes_checksum_and_content_type(self) -> None:
        store = await self.make_store()
        original = await store.put("src", b"payload", content_type="application/pdf")
        copied = await store.copy("src", "dst")
        assert copied.sha256 == original.sha256
        assert copied.content_type == original.content_type
        assert await store.get("dst") == b"payload"
        # Copy is a copy, not a move.
        assert await store.get("src") == b"payload"

    async def test_list_by_manifest_reports_presence_per_key(self) -> None:
        store = await self.make_store()
        await store.put("present-1", b"a")
        await store.put("present-2", b"bb")
        report = await store.list_by_manifest(["present-1", "missing", "present-2"])
        assert report["missing"] is None
        present_1 = report["present-1"]
        assert present_1 is not None and present_1.size == 1
        present_2 = report["present-2"]
        assert present_2 is not None and present_2.sha256 == sha256_hex(b"bb")

    async def test_signed_urls_are_key_specific_and_carry_expiry(self) -> None:
        store = await self.make_store()
        await store.put("a", b"1")
        await store.put("b", b"2")
        url_a = await store.signed_download_url("a", expires_in_seconds=60)
        url_b = await store.signed_download_url("b", expires_in_seconds=60)
        assert url_a.url != url_b.url, "a signed URL must not grant access to other keys"
        assert url_a.method == "GET"
        upload = await store.signed_upload_url("c", expires_in_seconds=60, content_type="text/csv")
        assert upload.method == "PUT"
        assert upload.expires_at.tzinfo is not None, "expiry must be timezone-aware"
        assert upload.expires_at > datetime.now(tz=UTC), "expiry must be in the future"

    async def test_signed_download_requires_an_existing_object(self) -> None:
        store = await self.make_store()
        with pytest.raises(ObjectNotFoundError):
            await store.signed_download_url("ghost", expires_in_seconds=60)

    async def test_list_keys_is_prefix_scoped_and_content_free(self) -> None:
        store = await self.make_store()
        await store.put("orgs/a/doc-1/original", b"one")
        await store.put("orgs/a/doc-2/original", b"two")
        await store.put("orgs/b/doc-9/original", b"three")
        assert await store.list_keys("orgs/a/") == [
            "orgs/a/doc-1/original",
            "orgs/a/doc-2/original",
        ]
        assert len(await store.list_keys()) == 3

    async def test_abort_multipart_is_idempotent(self) -> None:
        store = await self.make_store()
        # Aborting an unknown upload must be safe to retry.
        await store.abort_multipart("k", "upload-that-never-existed")
        await store.abort_multipart("k", "upload-that-never-existed")
