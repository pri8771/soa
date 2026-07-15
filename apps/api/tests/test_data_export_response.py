import uuid

from soa_api.routers.data_exports import _durable_response
from soa_db.data_export_jobs import DataExportState, new_data_export_job
from soa_db.types import utcnow
from soa_storage import MemoryObjectStore


async def test_durable_response_signs_parts_without_exposing_storage_keys_or_cursors() -> None:
    organization_id = uuid.uuid4()
    job = new_data_export_job(
        organization_id=organization_id,
        scope="organization",
        created_by="user:test",
    )
    job.id = uuid.uuid4()
    job.snapshot_at = utcnow()
    job.state = DataExportState.SUCCEEDED
    part_key = f"data-exports/{organization_id}/{job.id}/organization/roles/part-000001.json"
    manifest_key = f"data-exports/{organization_id}/{job.id}/manifest.json"
    job.parts = [
        {
            "part_type": "organization_records",
            "category": "roles",
            "object_key": part_key,
            "sha256": "a" * 64,
            "bytes": 2,
            "records": 0,
            "cursor": str(uuid.uuid4()),
            "complete": True,
        },
        {
            "part_type": "organization_records",
            "category": "workspaces",
            "records": 0,
            "cursor": None,
            "complete": True,
        },
    ]
    job.manifest_object_key = manifest_key
    store = MemoryObjectStore()
    await store.put(part_key, b"{}", content_type="application/json")
    await store.put(manifest_key, b"{}", content_type="application/json")

    response = await _durable_response(job, store, ttl=300)

    assert response["manifest_download_url"]
    assert len(response["parts"]) == 1
    assert response["parts"][0]["download_url"]
    assert "object_key" not in response["parts"][0]
    assert "cursor" not in response["parts"][0]
    assert "complete" not in response["parts"][0]
    assert "signature=" in response["parts"][0]["download_url"]
    assert "signature=" in response["manifest_download_url"]
