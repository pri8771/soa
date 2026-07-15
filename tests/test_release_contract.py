"""Static release contracts that fail before a broken deploy reaches GCP."""

import json
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


def test_migrator_runtime_contains_the_synchronous_postgres_driver() -> None:
    database_package = _read("packages/db/pyproject.toml")

    assert '"psycopg[binary]>=3.2"' in database_package


def test_deploy_promotes_independent_digests_and_correct_migration_env() -> None:
    workflow = _read(".github/workflows/deploy.yml")

    assert "release_run_id:" in workflow
    assert 'test "$(jq -r .conclusion' in workflow
    assert "release-manifest-${SOURCE_SHA}" in workflow
    for artifact in ("api", "worker", "migrator", "web"):
        assert f"steps.release.outputs.{artifact}_digest" in workflow
    assert "SOA_DATABASE_URL=" in workflow
    assert "migrator-database-url:latest" in workflow
    assert "SOA_API_DATABASE_URL=" not in workflow
    assert "gcloud run worker-pools deploy" in workflow
    assert "previous_image=" in workflow
    assert "Restore prior worker after a failed rollout" in workflow
    assert "--connect-timeout 2 --max-time 35" in workflow
    assert "hosting:channel:deploy" in workflow
    assert "hosting:clone" in workflow


def test_deploy_stages_web_then_promotes_and_rolls_back_every_runtime() -> None:
    workflow = _read(".github/workflows/deploy.yml")

    stage_web = workflow.index("name: Stage and smoke-test web candidate")
    deploy_worker = workflow.index("name: Deploy and verify worker pool")
    shift_api = workflow.index("name: Shift 100% traffic to the exact API candidate")
    canonical_smoke = workflow.index("name: Post-shift canonical API and authentication smoke")
    promote_web = workflow.index("name: Promote staged web version to Firebase live")
    web_smoke = workflow.index("name: Post-promotion web smoke")

    assert stage_web < deploy_worker < shift_api < canonical_smoke < promote_web < web_smoke
    assert "candidate_revision" in workflow
    assert "--to-latest" not in workflow
    assert "previous_traffic" in workflow
    assert ".revisionName == $revision and (.percent // 0) == 100" in workflow
    assert "candidate_version" in workflow
    assert "index_sha256" in workflow
    assert workflow.count("for attempt in $(seq 1 12)") >= 3
    assert "Restore prior Firebase live release after a failed rollout" in workflow
    assert "Restore prior API traffic after a failed rollout" in workflow
    assert "Restore prior worker after a failed rollout" in workflow
    assert "steps.promote_web.outputs.attempted == 'true'" in workflow
    assert "steps.promote_api.outputs.attempted == 'true'" in workflow
    assert "API_SMOKE_BEARER_TOKEN" in workflow
    assert '.error.code == "unauthorized"' in workflow
    assert 'grep -R -Fq -- "$API_SMOKE_BASE_URL" apps/web/dist/assets' in workflow


def test_release_publication_scans_every_promoted_artifact() -> None:
    workflow = _read(".github/workflows/release-images.yml")

    for artifact in ("api", "worker", "migrator", "web"):
        assert f"build_image {artifact} " in workflow
        assert f"steps.images.outputs.{artifact}_ref" in workflow
    assert "release-manifest.json" in workflow


def test_terraform_models_the_real_runtime_contract() -> None:
    compute = _read("infra/terraform/compute.tf")
    database = _read("infra/terraform/database.tf")
    secrets = _read("infra/terraform/secrets.tf")
    storage = _read("infra/terraform/storage.tf")
    monitoring = _read("infra/terraform/monitoring.tf")

    assert 'resource "google_cloud_run_v2_worker_pool" "worker"' in compute
    assert 'resource "google_cloud_run_v2_service" "worker"' not in compute
    assert "max_instance_request_concurrency = var.api_concurrency" in compute
    assert 'resource "google_sql_user" "app"' in database
    assert 'resource "google_sql_user" "migrator"' in database
    assert "tenant_secrets_project_id" in compute
    assert "project = var.tenant_secrets_project_id" in compute
    assert "value = var.tenant_secrets_project_id" in compute
    assert 'resource "google_project_service" "tenant_secret_manager"' in _read(
        "infra/terraform/main.tf"
    )
    assert "google_secret_manager_secret.migrator_database_url.id" in compute
    assert 'secret_id = "${local.name_prefix}-migrator-database-url"' in secrets
    assert "origin = var.api_cors_allowed_origins" in storage
    assert '"x-goog-meta-sha256"' in storage
    assert '"x-goog-content-length-range"' in storage
    for variable in (
        "SOA_API_AUTH_DEV_MODE",
        "SOA_API_OIDC_ISSUER",
        "SOA_API_CORS_ALLOWED_ORIGINS",
        "SOA_API_CLAMAV_HOST",
        "SOA_API_TELEMETRY_PROFILE",
        "SOA_API_OTLP_ENDPOINT",
        "SOA_API_EMAIL_INTAKE_SECRET",
        "SOA_API_EMAIL_INTAKE_MAX_BODY_BYTES",
        "SOA_API_EMAIL_INTAKE_MAX_TOTAL_ATTACHMENT_BYTES",
        "SOA_API_PUBLIC_INGEST_MAX_FILE_BYTES",
        "SOA_WORKER_STORAGE_BACKEND",
        "SOA_WORKER_MAX_CONCURRENCY",
        "SOA_WORKER_EXPORT_DESTINATION_ALLOWLIST",
        "SOA_WORKER_OUTBOX_PUBLISH_URL",
        "SOA_WORKER_OUTBOX_SIGNING_SECRET",
        "SOA_WORKER_TELEMETRY_PROFILE",
        "SOA_WORKER_OTLP_ENDPOINT",
    ):
        assert variable in compute
    terraform_variables = _read("infra/terraform/variables.tf")
    assert 'variable "api_concurrency"' in terraform_variables
    assert 'variable "telemetry_otlp_endpoint"' in terraform_variables
    assert "REPLACE_WITH_API_HOST" not in monitoring


def test_migrations_grant_dml_without_runtime_schema_ownership() -> None:
    migration = _read("migrations/versions/0045_runtime_database_grants.py")

    assert 'RUNTIME_ROLE = "soa_app"' in migration
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" in migration
    assert "ALTER DEFAULT PRIVILEGES" in migration
    assert "REVOKE {CLOUD_SQL_ADMIN_ROLE} FROM {RUNTIME_ROLE}" in migration
    assert "NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION" in migration
    assert "GRANT CREATE" not in migration


def test_firebase_hosting_keeps_the_production_security_boundary() -> None:
    config = _read("firebase.json")

    assert "Content-Security-Policy" in config
    assert "frame-ancestors 'none'" in config
    assert '"destination": "/index.html"' in config


def test_web_hosts_share_transport_and_cross_origin_security_headers() -> None:
    vite = _read("apps/web/vite.config.ts")
    nginx = _read("apps/web/nginx.conf")
    firebase = _read("firebase.json")
    surfaces = (vite, nginx, firebase)
    for config in surfaces:
        assert "Strict-Transport-Security" in config
        assert "max-age=31536000; includeSubDomains" in config
        assert "Cross-Origin-Opener-Policy" in config
        assert "same-origin-allow-popups" in config
        assert "Cross-Origin-Resource-Policy" in config
        assert "same-origin" in config

    assert '"Cross-Origin-Resource-Policy": "same-origin"' in vite
    assert 'add_header Cross-Origin-Resource-Policy "same-origin" always;' in nginx
    hosting = json.loads(firebase)["hosting"]
    global_headers = next(rule for rule in hosting["headers"] if rule["source"] == "**")
    firebase_headers = {header["key"]: header["value"] for header in global_headers["headers"]}
    assert firebase_headers["Strict-Transport-Security"] == ("max-age=31536000; includeSubDomains")
    assert firebase_headers["Cross-Origin-Opener-Policy"] == "same-origin-allow-popups"
    assert firebase_headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert nginx.count("Strict-Transport-Security") == 2
    assert "default_type text/plain" in nginx


def test_production_trace_export_is_off_the_request_path() -> None:
    telemetry = _read("packages/config/src/soa_config/telemetry.py")

    assert 'if profile == "otlp" and span_exporter is None' in telemetry
    assert "BatchSpanProcessor(exporter)" in telemetry
