"""Development filesystem URLs are bounded and claim-bound."""

from pathlib import Path

from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_storage.filesystem import FilesystemObjectStore


async def test_local_upload_binds_content_type_and_caps_streamed_bytes(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path, base_url="http://testserver/_local-blobs")
    app = create_app(
        ApiSettings(environment=Environment.TEST, max_upload_bytes=4),
        object_store=store,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        correct = await store.signed_upload_url(
            "org/doc/original.pdf",
            expires_in_seconds=60,
            content_type="application/pdf",
            size_bytes=4,
        )
        wrong_type = client.put(
            correct.url,
            content=b"1234",
            headers={"Content-Type": "image/png"},
        )
        assert wrong_type.status_code == 403

        wrong_size = client.put(
            correct.url,
            content=b"123",
            headers={"Content-Type": "application/pdf"},
        )
        assert wrong_size.status_code == 422

        too_large = client.put(
            correct.url,
            content=b"12345",
            headers={"Content-Type": "application/pdf"},
        )
        assert too_large.status_code == 413

        uploaded = client.put(
            correct.url,
            content=b"1234",
            headers={"Content-Type": "application/pdf"},
        )
        assert uploaded.status_code == 200
        assert await store.get("org/doc/original.pdf") == b"1234"
