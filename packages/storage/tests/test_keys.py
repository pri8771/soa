"""Artifact keys are tenant/document scoped and non-guessable (STO-002)."""

import uuid

import pytest

from soa_storage.keys import artifact_key, safe_filename

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
DOC = uuid.UUID("22222222-2222-4222-8222-222222222222")


def test_keys_are_scoped_to_tenant_and_document() -> None:
    key = artifact_key(ORG, DOC, kind="original", filename="po-4711.pdf")
    assert key.startswith(f"orgs/{ORG}/documents/{DOC}/original/")
    assert key.endswith("-po-4711.pdf")


def test_keys_are_non_guessable() -> None:
    # Same inputs, different keys: the 128-bit token makes enumeration
    # of sibling artifacts impossible from a leaked key.
    first = artifact_key(ORG, DOC, kind="original", filename="a.pdf")
    second = artifact_key(ORG, DOC, kind="original", filename="a.pdf")
    assert first != second
    token = first.rsplit("/", 1)[1].removesuffix("-a.pdf")
    assert len(token) >= 22  # token_urlsafe(16) -> 22 chars


def test_filenames_cannot_traverse_paths() -> None:
    assert "/" not in safe_filename("../../etc/passwd")
    assert safe_filename("..\\..\\boot.ini") == "boot.ini"
    assert safe_filename("Ünïcode nãme.PDF") == "n-code-n-me.PDF"
    assert safe_filename("///") == "file"
    key = artifact_key(ORG, DOC, kind="original", filename="../escape.pdf")
    assert f"orgs/{ORG}/documents/{DOC}/original/" in key
    assert ".." not in key


def test_kind_must_be_a_slug() -> None:
    with pytest.raises(ValueError, match="lowercase slug"):
        artifact_key(ORG, DOC, kind="Not A Slug!")
