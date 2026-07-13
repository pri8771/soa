"""Integration models — moved to soa_db.integrations (EXP-008) so the
export worker can load integrations; this shim keeps imports stable."""

from soa_db.integrations import (
    INTEGRATION_TYPES,
    Integration,
    IntegrationCredential,
    IntegrationCredentialRepository,
    IntegrationRepository,
    IntegrationStatus,
    MappingProfileVersion,
    MappingProfileVersionRepository,
    UnknownIntegrationTypeError,
    create_integration,
    create_mapping_draft,
    credential_secret_for_delivery,
    publish_mapping_draft,
    store_integration_credential,
)

__all__ = [
    "INTEGRATION_TYPES",
    "Integration",
    "IntegrationCredential",
    "IntegrationCredentialRepository",
    "IntegrationRepository",
    "IntegrationStatus",
    "MappingProfileVersion",
    "MappingProfileVersionRepository",
    "UnknownIntegrationTypeError",
    "create_integration",
    "create_mapping_draft",
    "credential_secret_for_delivery",
    "publish_mapping_draft",
    "store_integration_credential",
]
