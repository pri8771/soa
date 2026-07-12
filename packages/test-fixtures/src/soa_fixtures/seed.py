"""Seed runner over a pluggable sink.

The runner applies the demo tenant through ``SeedSink.upsert`` — idempotent
by contract, so repeated seeding converges instead of duplicating. The
database-backed sink arrives with the DB epic; ``InMemorySeedSink`` supports
tests and dry runs today.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from soa_fixtures.demo import DEMO_TENANT, DemoTenant, Fixture


class SeedSink(Protocol):
    def upsert(self, kind: str, fixture_id: uuid.UUID, alias: str, data: dict[str, Any]) -> None:
        """Create or replace the record for (kind, fixture_id)."""
        ...


@dataclass
class SeedReport:
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass
class InMemorySeedSink:
    records: dict[tuple[str, uuid.UUID], dict[str, Any]] = field(default_factory=dict)
    aliases: dict[str, uuid.UUID] = field(default_factory=dict)

    def upsert(self, kind: str, fixture_id: uuid.UUID, alias: str, data: dict[str, Any]) -> None:
        self.records[(kind, fixture_id)] = {"alias": alias, **data}
        self.aliases[alias] = fixture_id

    def get_by_alias(self, alias: str) -> dict[str, Any] | None:
        fixture_id = self.aliases.get(alias)
        if fixture_id is None:
            return None
        for (_, record_id), record in self.records.items():
            if record_id == fixture_id:
                return record
        return None


class SeedRunner:
    def __init__(self, tenant: DemoTenant = DEMO_TENANT) -> None:
        self._tenant = tenant

    def seed(self, sink: SeedSink) -> SeedReport:
        counts: Counter[str] = Counter()

        def apply(kind: str, fixture: Fixture, extra: dict[str, Any] | None = None) -> None:
            payload = {**fixture.data, **(extra or {})}
            sink.upsert(kind, fixture.id, fixture.alias, payload)
            counts[kind] += 1

        tenant = self._tenant
        apply("organization", tenant.organization)
        apply("workspace", tenant.workspace, {"organization_id": str(tenant.organization.id)})
        apply("process", tenant.process, {"workspace_id": str(tenant.workspace.id)})
        for stream in tenant.streams:
            apply("stream", stream, {"process_id": str(tenant.process.id)})
        for user in tenant.users:
            apply("user", user, {"organization_id": str(tenant.organization.id)})
        for catalog in tenant.catalogs:
            apply("catalog", catalog, {"organization_id": str(tenant.organization.id)})
        apply("schema", tenant.schema, {"process_id": str(tenant.process.id)})
        apply("rule_set", tenant.rules, {"process_id": str(tenant.process.id)})
        apply("provider_policy", tenant.provider_policy, {"process_id": str(tenant.process.id)})
        for document in tenant.documents:
            apply(
                "document",
                document,
                {
                    "organization_id": str(tenant.organization.id),
                    "scenario": document.scenario,
                    "text": document.text,
                    "expected_fields": document.expected_fields,
                },
            )
        return SeedReport(counts=dict(counts))
