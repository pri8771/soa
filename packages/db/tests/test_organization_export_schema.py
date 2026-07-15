from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from soa_db.organization_export import ORGANIZATION_EXPORT_CATEGORIES

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_CONTROL_PLANE_CATEGORIES = {
    "audit_events",
    "catalog_bindings",
    "catalog_records",
    "catalog_versions",
    "catalogs",
    "data_export_jobs",
    "deletion_tombstones",
    "deletion_requests",
    "evaluation_runs",
    "external_cleanup_intents",
    "feature_flags",
    "gold_dataset_versions",
    "gold_datasets",
    "gold_documents",
    "instruction_versions",
    "integration_credentials",
    "integrations",
    "jobs",
    "legal_holds",
    "mapping_profile_versions",
    "memberships",
    "organization",
    "outbox_events",
    "policy_versions",
    "provider_credentials",
    "provider_runtime_metrics",
    "process_versions",
    "processes",
    "role_assignments",
    "roles",
    "rule_set_versions",
    "schema_versions",
    "service_credentials",
    "stream_versions",
    "streams",
    "upload_sessions",
    "usage_ledger_entries",
    "users",
    "workspaces",
}


def test_export_allowlist_matches_every_migrated_tenant_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path}/export-schema.db"
    monkeypatch.setenv("SOA_DATABASE_URL", url)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {category.key for category in ORGANIZATION_EXPORT_CATEGORIES} == (
        EXPECTED_CONTROL_PLANE_CATEGORIES
    )
    for category in ORGANIZATION_EXPORT_CATEGORIES:
        assert category.table_name in table_names, category.key
        columns = {column["name"] for column in inspector.get_columns(category.table_name)}
        assert "id" in columns, category.key
        assert category.snapshot_column in columns, category.key
        if category.scope == "organization_id":
            assert "organization_id" in columns, category.key
    engine.dispose()
