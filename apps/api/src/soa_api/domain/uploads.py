"""Upload sessions (ING-002).

An upload session is the contract between a client that wants to send a
file and the platform: the client declares filename, type, size, and
SHA-256 up front; the platform validates policy (supported type, size
cap, pending-session quota) BEFORE signing anything; the client PUTs the
bytes to a short-lived signed URL; completion verifies the stored
object's metadata against the declaration and registers the document
exactly once. Sessions expire lazily — a session past its deadline flips
to expired the moment anyone touches it.

The document id is allocated at session creation so the object key is
final from the start (tenant/document-scoped, non-guessable) — no
copy-on-complete, no orphan renames.
"""

from soa_db.uploads import (
    UPLOAD_CLEANUP_GRACE,
    UPLOAD_CLEANUP_JOB_TYPE,
    UPLOAD_CLEANUP_SWEEP_GRACE,
    UPLOAD_VERIFICATION_LEASE,
    InvalidUploadSessionStateError,
    UploadSession,
    UploadSessionRepository,
    UploadSessionState,
    create_upload_session,
    is_expired,
)

#: Types accepted for upload. Deep content inspection (signature vs
#: extension, ING-003) happens later in the pipeline; this is the intake
#: policy gate.
SUPPORTED_UPLOAD_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/tiff",
    }
)


class UploadPolicyError(Exception):
    """The declaration violates intake policy (type/size/quota)."""


def validate_upload_declaration(
    *,
    content_type: str,
    size_bytes: int,
    pending_sessions: int,
    max_size_bytes: int,
    max_pending_sessions: int,
) -> None:
    """Policy gate, evaluated BEFORE any URL is signed."""
    if content_type not in SUPPORTED_UPLOAD_TYPES:
        supported = ", ".join(sorted(SUPPORTED_UPLOAD_TYPES))
        raise UploadPolicyError(
            f"unsupported content type {content_type!r}; supported: {supported}"
        )
    if size_bytes <= 0:
        raise UploadPolicyError("declared size must be positive")
    if size_bytes > max_size_bytes:
        raise UploadPolicyError(
            f"declared size {size_bytes} exceeds the {max_size_bytes}-byte limit"
        )
    if pending_sessions >= max_pending_sessions:
        raise UploadPolicyError(
            f"too many pending upload sessions ({pending_sessions}); "
            "complete or abort existing sessions first"
        )


__all__ = [
    "SUPPORTED_UPLOAD_TYPES",
    "UPLOAD_CLEANUP_GRACE",
    "UPLOAD_CLEANUP_JOB_TYPE",
    "UPLOAD_CLEANUP_SWEEP_GRACE",
    "UPLOAD_VERIFICATION_LEASE",
    "InvalidUploadSessionStateError",
    "UploadPolicyError",
    "UploadSession",
    "UploadSessionRepository",
    "UploadSessionState",
    "create_upload_session",
    "is_expired",
    "validate_upload_declaration",
]
