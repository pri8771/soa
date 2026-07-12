"""Deterministic demo tenant fixtures and seed runner.

Fixture IDs are derived from stable aliases (uuid5), so every seed run in
every environment produces identical identifiers — E2E tests locate records
by alias without scraping.
"""

from soa_fixtures.demo import DEMO_TENANT, DemoTenant
from soa_fixtures.ids import stable_id
from soa_fixtures.seed import InMemorySeedSink, SeedReport, SeedRunner, SeedSink

__all__ = [
    "DEMO_TENANT",
    "DemoTenant",
    "InMemorySeedSink",
    "SeedReport",
    "SeedRunner",
    "SeedSink",
    "stable_id",
]
