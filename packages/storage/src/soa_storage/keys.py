"""Artifact key construction (STO-002).

Keys are tenant/document scoped and non-guessable: every key embeds a
random token, so possessing one signed URL never lets a client enumerate
sibling objects, and a leaked key for one document reveals nothing about
any other document or tenant.
"""

import re
import secrets
import uuid

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(filename: str) -> str:
    """Strip path separators and anything not filesystem-tame. The result
    is display metadata only — uniqueness comes from the token."""
    cleaned = _SAFE_FILENAME.sub("-", filename.replace("\\", "/").split("/")[-1])
    return cleaned.strip("-.") or "file"


def artifact_key(
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    kind: str,
    filename: str = "artifact",
) -> str:
    """``orgs/{org}/documents/{doc}/{kind}/{token}-{filename}``. The token
    is 128 bits of randomness — keys are unguessable by construction."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", kind):
        raise ValueError(f"artifact kind {kind!r} must be a lowercase slug")
    token = secrets.token_urlsafe(16)
    return (
        f"orgs/{organization_id}/documents/{document_id}/{kind}/{token}-{safe_filename(filename)}"
    )
