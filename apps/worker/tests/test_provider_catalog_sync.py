"""Catalog/registry drift guard (AIO-019): the shared soa_config
provider catalog and the worker's live registry describe the SAME
providers — a new adapter or a changed data policy must land in both,
or this fails."""

import shutil

from soa_config.provider_catalog import PROVIDER_CATALOG
from soa_worker.providers import Capability, registered_providers

CATALOG_BY_KEY = {(entry.capability, entry.name): entry for entry in PROVIDER_CATALOG}


def test_every_registered_provider_is_declared_in_the_catalog() -> None:
    for info in registered_providers():
        entry = CATALOG_BY_KEY.get((info.capability.value, info.name))
        assert entry is not None, (
            f"{info.capability.value}/{info.name} is registered but missing from "
            "soa_config.provider_catalog — the admin UI would not show it"
        )
        assert entry.processing_region == info.data_policy.processing_region
        assert entry.sends_content_to_third_party == info.data_policy.sends_content_to_third_party
        assert entry.retains_content == info.data_policy.retains_content
        assert entry.uses_content_for_training == info.data_policy.uses_content_for_training


def test_always_available_catalog_entries_are_actually_registered() -> None:
    registered = {(info.capability.value, info.name) for info in registered_providers()}
    for entry in PROVIDER_CATALOG:
        if entry.availability == "always":
            assert (entry.capability, entry.name) in registered, (
                f"catalog promises {entry.name} is always available, but it is not registered"
            )


def test_tesseract_languages_cover_the_catalog_declaration() -> None:
    if not shutil.which("tesseract"):
        import pytest

        pytest.skip("tesseract binary not installed")
    (info,) = [i for i in registered_providers(Capability.OCR) if i.name == "tesseract"]
    entry = CATALOG_BY_KEY[("ocr", "tesseract")]
    # The deployment must install at least the packs the catalog declares.
    assert set(entry.languages) <= set(info.languages)
