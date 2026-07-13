"""File signature and media validation (ING-003).

Trust the BYTES, not the label: the actual content type is detected from
file signatures (magic bytes) and compared against both the declared
content type and the filename extension. A file that claims to be a PDF
but carries PNG bytes — or any other type confusion — is rejected; a
file whose signature no detector recognizes is quarantined as
potentially malformed or crafted. Reasons are safe to display: they name
types and extensions, never file content.

This runs synchronously at upload completion today; ING-004/007 move
scanning/inspection onto worker jobs, reusing these verdicts.
"""

from dataclasses import dataclass
from enum import StrEnum

from soa_api.domain.uploads import SUPPORTED_UPLOAD_TYPES

#: Signature table: prefix -> detected media type. Checked in order;
#: first match wins. TIFF has two byte orders.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
]

#: How many leading bytes a caller must provide for detection.
SNIFF_LENGTH = 16

_EXTENSION_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


class InspectionVerdict(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"  # recognizable but wrong/unsupported: user-fixable
    QUARANTINED = "quarantined"  # unrecognizable/deceptive: handled with care


@dataclass(frozen=True)
class InspectionResult:
    verdict: InspectionVerdict
    detected_type: str | None
    #: Safe for UI/audit: names types and extensions, never content.
    reason: str | None


def detect_content_type(head: bytes) -> str | None:
    for prefix, media_type in _SIGNATURES:
        if head.startswith(prefix):
            return media_type
    return None


def _extension(filename: str) -> str | None:
    name = filename.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower()


def inspect_file(*, head: bytes, declared_type: str, filename: str) -> InspectionResult:
    """Compare detected content against declaration and extension."""
    detected = detect_content_type(head[:SNIFF_LENGTH])
    if detected is None:
        return InspectionResult(
            verdict=InspectionVerdict.QUARANTINED,
            detected_type=None,
            reason="file signature is not recognized as any supported media type",
        )
    if detected not in SUPPORTED_UPLOAD_TYPES:
        return InspectionResult(
            verdict=InspectionVerdict.REJECTED,
            detected_type=detected,
            reason=f"detected type {detected} is not supported",
        )
    if detected != declared_type:
        return InspectionResult(
            verdict=InspectionVerdict.REJECTED,
            detected_type=detected,
            reason=(
                f"content is {detected} but was declared as {declared_type} — "
                "type confusion is refused"
            ),
        )
    extension = _extension(filename)
    extension_type = _EXTENSION_TYPES.get(extension or "")
    if extension is None or extension_type is None:
        return InspectionResult(
            verdict=InspectionVerdict.REJECTED,
            detected_type=detected,
            reason=f"filename extension {extension or '(none)'!s} is not a supported type",
        )
    if extension_type != detected:
        return InspectionResult(
            verdict=InspectionVerdict.REJECTED,
            detected_type=detected,
            reason=(
                f"filename extension .{extension} does not match the detected "
                f"content type {detected}"
            ),
        )
    return InspectionResult(verdict=InspectionVerdict.PASSED, detected_type=detected, reason=None)
