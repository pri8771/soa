"""GCS ObjectStore adapter tests (STO-002 for GCP).

A faithful in-memory fake of the google-cloud-storage client exercises
the adapter's real code paths — checksum write/verify, not-found and
loud-delete semantics, copy, listing, and signed-URL shape — without a
live bucket. (The true end-to-end proof that a signed URL grants access
runs against a real bucket, gated like the S3/MinIO contract test.)
"""

from datetime import UTC, datetime

import pytest
from google.api_core import exceptions as gcp_exceptions

from soa_storage.gcs import GcsObjectStore, GcsSettings
from soa_storage.store import (
    ChecksumMismatchError,
    ObjectNotFoundError,
    sha256_hex,
)

FIXED_DT = datetime(2026, 7, 14, tzinfo=UTC)


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", key: str, kms_key_name: str | None = None) -> None:
        self._bucket = bucket
        self.name = key
        self.kms_key_name = kms_key_name
        self.metadata: dict | None = None
        self.content_type: str | None = None
        self._data: bytes | None = None
        self.size: int | None = None
        self.time_created: datetime | None = None

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._bucket.objects[self.name] = {
            "data": bytes(data),
            "metadata": dict(self.metadata or {}),
            "content_type": content_type,
            "kms": self.kms_key_name,
            "time_created": FIXED_DT,
        }

    def download_as_bytes(self) -> bytes:
        assert self._data is not None
        return self._data

    def delete(self) -> None:
        if self.name not in self._bucket.objects:
            raise gcp_exceptions.NotFound(self.name)
        del self._bucket.objects[self.name]

    def generate_signed_url(
        self, *, version: str, expiration: object, method: str, content_type: str | None = None
    ) -> str:
        return f"https://signed.example/{self._bucket.name}/{self.name}?method={method}"


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects: dict[str, dict] = {}

    def blob(self, key: str, kms_key_name: str | None = None) -> FakeBlob:
        return FakeBlob(self, key, kms_key_name)

    def get_blob(self, key: str) -> FakeBlob | None:
        record = self.objects.get(key)
        if record is None:
            return None
        blob = FakeBlob(self, key)
        blob._data = record["data"]
        blob.metadata = dict(record["metadata"])
        blob.content_type = record["content_type"]
        blob.size = len(record["data"])
        blob.time_created = record["time_created"]
        return blob

    def copy_blob(self, source_blob: FakeBlob, dest_bucket: "FakeBucket", dest_key: str) -> None:
        dest_bucket.objects[dest_key] = dict(self.objects[source_blob.name])


class FakeGcsClient:
    def __init__(self) -> None:
        self._buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self._buckets.setdefault(name, FakeBucket(name))

    def list_blobs(self, bucket_name: str, prefix: str = "") -> list[object]:
        bucket = self._buckets.get(bucket_name)
        if bucket is None:
            return []
        return [type("B", (), {"name": key})() for key in bucket.objects if key.startswith(prefix)]


def make_store(kms: str | None = None) -> GcsObjectStore:
    settings = GcsSettings(bucket="soa-docs", project="soa-pilot", kms_key_name=kms)
    return GcsObjectStore(settings, client=FakeGcsClient())


class TestSettings:
    def test_bucket_and_project_required(self) -> None:
        with pytest.raises(ValueError, match="bucket"):
            GcsSettings(bucket="  ", project="p")
        with pytest.raises(ValueError, match="project"):
            GcsSettings(bucket="b", project="")


class TestRoundTrip:
    async def test_put_then_get_returns_bytes(self) -> None:
        store = make_store()
        meta = await store.put("docs/a.pdf", b"hello", content_type="application/pdf")
        assert meta.sha256 == sha256_hex(b"hello")
        assert meta.size == 5
        assert await store.get("docs/a.pdf") == b"hello"

    async def test_head_reports_metadata_without_content(self) -> None:
        store = make_store()
        await store.put("docs/a.pdf", b"hello", content_type="application/pdf")
        meta = await store.head("docs/a.pdf")
        assert meta.content_type == "application/pdf"
        assert meta.sha256 == sha256_hex(b"hello")

    async def test_declared_checksum_mismatch_refuses_write(self) -> None:
        store = make_store()
        with pytest.raises(ChecksumMismatchError):
            await store.put("k", b"hello", sha256="0" * 64)

    async def test_corrupted_stored_checksum_is_caught_on_get(self) -> None:
        store = make_store()
        await store.put("k", b"hello")
        # Corrupt the stored bytes behind the adapter's back.
        store._client.bucket("soa-docs").objects["k"]["data"] = b"tampered"
        with pytest.raises(ChecksumMismatchError):
            await store.get("k")


class TestMissing:
    async def test_get_missing_is_not_found(self) -> None:
        with pytest.raises(ObjectNotFoundError):
            await make_store().get("nope")

    async def test_head_missing_is_not_found(self) -> None:
        with pytest.raises(ObjectNotFoundError):
            await make_store().head("nope")

    async def test_delete_missing_is_loud(self) -> None:
        with pytest.raises(ObjectNotFoundError):
            await make_store().delete("nope")

    async def test_delete_removes_the_object(self) -> None:
        store = make_store()
        await store.put("k", b"x")
        await store.delete("k")
        with pytest.raises(ObjectNotFoundError):
            await store.head("k")


class TestCopyAndList:
    async def test_copy_preserves_checksum_and_type(self) -> None:
        store = make_store()
        await store.put("src", b"payload", content_type="text/plain")
        meta = await store.copy("src", "dst")
        assert meta.sha256 == sha256_hex(b"payload")
        assert await store.get("dst") == b"payload"

    async def test_copy_missing_source_is_not_found(self) -> None:
        with pytest.raises(ObjectNotFoundError):
            await make_store().copy("missing", "dst")

    async def test_list_keys_is_sorted_and_prefix_filtered(self) -> None:
        store = make_store()
        await store.put("a/2", b"x")
        await store.put("a/1", b"x")
        await store.put("b/1", b"x")
        assert await store.list_keys("a/") == ["a/1", "a/2"]
        assert await store.list_keys() == ["a/1", "a/2", "b/1"]

    async def test_manifest_reports_presence(self) -> None:
        store = make_store()
        await store.put("present", b"x")
        report = await store.list_by_manifest(["present", "absent"])
        assert report["present"] is not None
        assert report["absent"] is None


class TestSignedUrls:
    async def test_upload_url_has_put_method_and_expiry(self) -> None:
        signed = await make_store().signed_upload_url(
            "k", expires_in_seconds=300, content_type="application/pdf"
        )
        assert signed.method == "PUT"
        assert "method=PUT" in signed.url
        assert signed.expires_at > FIXED_DT.replace(year=2020)

    async def test_download_url_requires_the_object_to_exist(self) -> None:
        store = make_store()
        with pytest.raises(ObjectNotFoundError):
            await store.signed_download_url("missing", expires_in_seconds=300)
        await store.put("k", b"x")
        signed = await store.signed_download_url("k", expires_in_seconds=300)
        assert signed.method == "GET"


class TestKms:
    async def test_kms_key_flows_to_the_blob(self) -> None:
        store = make_store(kms="projects/p/locations/l/keyRings/r/cryptoKeys/c")
        await store.put("k", b"x")
        record = store._client.bucket("soa-docs").objects["k"]
        assert record["kms"] == "projects/p/locations/l/keyRings/r/cryptoKeys/c"


class TestMultipart:
    async def test_abort_multipart_is_a_safe_noop(self) -> None:
        # GCS has no S3-style multipart id; abort must be a safe no-op.
        await make_store().abort_multipart("k", "unknown-upload-id")
