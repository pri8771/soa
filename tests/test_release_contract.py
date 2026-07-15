"""Static release contracts that fail before a broken deploy reaches GCP."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_api_and_migrator_are_distinct_container_targets() -> None:
    dockerfile = _read("apps/api/Dockerfile")

    assert "FROM runtime-base AS migrator" in dockerfile
    assert "COPY --chown=soa:soa alembic.ini" in dockerfile
    assert "COPY --chown=soa:soa migrations" in dockerfile
    assert "FROM runtime-base AS api" in dockerfile


def test_deploy_promotes_independent_digests_and_correct_migration_env() -> None:
    workflow = _read(".github/workflows/deploy.yml")

    assert "release_run_id:" in workflow
    assert 'test "$(jq -r .conclusion' in workflow
    assert "release-manifest-${SOURCE_SHA}" in workflow
    for artifact in ("api", "worker", "migrator", "web"):
        assert f"steps.release.outputs.{artifact}_digest" in workflow
    assert "SOA_DATABASE_URL=" in workflow
    assert "SOA_API_DATABASE_URL=" not in workflow
    assert "gcloud run worker-pools deploy" in workflow
    assert "firebase-tools@15.23.0 deploy" in workflow


def test_release_publication_scans_every_promoted_artifact() -> None:
    workflow = _read(".github/workflows/release-images.yml")

    for artifact in ("api", "worker", "migrator", "web"):
        assert f"build_image {artifact} " in workflow
        assert f"steps.images.outputs.{artifact}_ref" in workflow
    assert "release-manifest.json" in workflow


def test_terraform_models_the_real_runtime_contract() -> None:
    compute = _read("infra/terraform/compute.tf")
    monitoring = _read("infra/terraform/monitoring.tf")

    assert 'resource "google_cloud_run_v2_worker_pool" "worker"' in compute
    assert 'resource "google_cloud_run_v2_service" "worker"' not in compute
    for variable in (
        "SOA_API_AUTH_DEV_MODE",
        "SOA_API_OIDC_ISSUER",
        "SOA_API_CORS_ALLOWED_ORIGINS",
        "SOA_API_CLAMAV_HOST",
        "SOA_WORKER_STORAGE_BACKEND",
        "SOA_WORKER_EXPORT_DESTINATION_ALLOWLIST",
        "SOA_WORKER_OUTBOX_PUBLISH_URL",
        "SOA_WORKER_OUTBOX_SIGNING_SECRET",
    ):
        assert variable in compute
    assert "REPLACE_WITH_API_HOST" not in monitoring


def test_firebase_hosting_keeps_the_production_security_boundary() -> None:
    config = _read("firebase.json")

    assert "Content-Security-Policy" in config
    assert "frame-ancestors 'none'" in config
    assert '"destination": "/index.html"' in config
