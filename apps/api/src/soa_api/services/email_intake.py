"""Email intake (ING-014).

Provider-neutral: every inbound adapter (the local Mailpit poller, a
production SES/SendGrid webhook later) reduces a message to raw MIME
bytes; ``parse_inbound_email`` turns MIME into the ``InboundEmail``
contract and ``process_inbound_email`` routes and ingests it. The
message BODY is never stored — only allowlisted metadata (sender,
subject, message id) rides along on the documents, per the ING-001
source-metadata policy.

Routing: the recipient's local part addresses a tenant stream as
``<org-slug>.<stream-slug>@…`` (e.g. ``northstar.email@intake.example``).
Unroutable messages are reported, never guessed.

Loop prevention: auto-generated mail (Auto-Submitted other than "no",
Precedence bulk/junk/list, X-Auto-Response-Suppress) is skipped before
any processing, so a bounce or vacation reply can never ingest
documents — and since this service only ingests and never replies,
no mail loop can amplify.

Every supported attachment becomes its own document through the shared
intake pipeline (limits, inspection, scan, duplicates, registration).
Unsupported attachments and attachment-less messages produce explicit
per-item outcomes in the report, not silence.
"""

from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.domain.streams import Stream, StreamRepository, StreamStatus
from soa_api.domain.tenancy import Organization
from soa_api.domain.uploads import SUPPORTED_UPLOAD_TYPES
from soa_api.services.ingestion import IntakeDeclaration, finalize_document_intake
from soa_api.services.malware import MalwareScanner
from soa_api.services.runtime_pins import RuntimePinError, resolve_runtime_pins
from soa_db.audit import ActorType
from soa_db.documents import SourceChannel
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import uuid7
from soa_storage import ObjectStore, sha256_hex
from soa_storage.keys import artifact_key


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class InboundEmail:
    sender: str
    recipient: str
    subject: str
    message_id: str | None
    attachments: list[EmailAttachment]
    #: True when headers mark the message as auto-generated.
    auto_generated: bool


@dataclass(frozen=True)
class AttachmentOutcome:
    filename: str
    outcome: str  # "ingested" | "unsupported" | "empty"
    document_id: str | None = None
    state: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class EmailIntakeReport:
    outcome: str  # "processed" | "skipped"
    reason: str | None = None
    attachments: list[AttachmentOutcome] = field(default_factory=list)


def _is_auto_generated(message: Message) -> bool:
    auto_submitted = (message.get("Auto-Submitted") or "no").strip().lower()
    if auto_submitted not in ("", "no"):
        return True
    precedence = (message.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "junk", "list", "auto_reply"):
        return True
    return message.get("X-Auto-Response-Suppress") is not None


def parse_inbound_email(raw: bytes) -> InboundEmail:
    message = message_from_bytes(raw)
    attachments: list[EmailAttachment] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue  # body text/html: deliberately never retained
        decoded = part.get_payload(decode=True)
        payload = decoded if isinstance(decoded, bytes) else b""
        attachments.append(
            EmailAttachment(
                filename=filename or "attachment",
                content_type=part.get_content_type(),
                data=payload,
            )
        )
    _, sender = parseaddr(str(message.get("From", "")))
    _, recipient = parseaddr(str(message.get("To", "")))
    return InboundEmail(
        sender=sender,
        recipient=recipient,
        subject=str(message.get("Subject", "")),
        message_id=(str(message["Message-ID"]).strip() if message.get("Message-ID") else None),
        attachments=attachments,
        auto_generated=_is_auto_generated(message),
    )


def route_recipient(recipient: str) -> tuple[str, str] | None:
    """``<org-slug>.<stream-slug>@…`` -> (org_slug, stream_slug)."""
    local, _, domain = recipient.partition("@")
    if not domain:
        return None
    org_slug, separator, stream_slug = local.partition(".")
    if not separator or not org_slug or not stream_slug:
        return None
    return org_slug.lower(), stream_slug.lower()


async def _resolve_stream(
    session: AsyncSession, org_slug: str, stream_slug: str
) -> tuple[OrganizationContext, Stream] | None:
    organization = (
        await session.execute(select(Organization).where(Organization.slug == org_slug))
    ).scalar_one_or_none()
    if organization is None:
        return None
    await bind_tenant(session, organization.id)
    context = OrganizationContext(organization_id=organization.id)
    stream = await StreamRepository(session, context).get_by_slug(stream_slug)
    if stream is None or stream.status == StreamStatus.ARCHIVED:
        return None
    return context, stream


async def process_inbound_email(
    session: AsyncSession,
    *,
    email: InboundEmail,
    store: ObjectStore,
    scanner: MalwareScanner,
) -> EmailIntakeReport:
    if email.auto_generated:
        return EmailIntakeReport(
            outcome="skipped", reason="auto-generated message (loop prevention)"
        )
    route = route_recipient(email.recipient)
    if route is None:
        return EmailIntakeReport(
            outcome="skipped",
            reason=f"recipient {email.recipient!r} does not address a stream",
        )
    resolved = await _resolve_stream(session, *route)
    if resolved is None:
        return EmailIntakeReport(
            outcome="skipped",
            reason=f"no active stream for recipient {email.recipient!r}",
        )
    context, stream = resolved
    try:
        pins = await resolve_runtime_pins(
            session,
            context,
            stream_version_id=stream.active_version_id,
        )
    except RuntimePinError as error:
        return EmailIntakeReport(outcome="skipped", reason=str(error))

    if not email.attachments:
        return EmailIntakeReport(outcome="processed", reason="no attachments", attachments=[])

    outcomes: list[AttachmentOutcome] = []
    for attachment in email.attachments:
        if not attachment.data:
            outcomes.append(
                AttachmentOutcome(
                    filename=attachment.filename, outcome="empty", detail="attachment is empty"
                )
            )
            continue
        if attachment.content_type not in SUPPORTED_UPLOAD_TYPES:
            outcomes.append(
                AttachmentOutcome(
                    filename=attachment.filename,
                    outcome="unsupported",
                    detail=f"unsupported content type {attachment.content_type}",
                )
            )
            continue
        document_id = uuid7()
        key = artifact_key(
            context.organization_id, document_id, kind="original", filename=attachment.filename
        )
        digest = sha256_hex(attachment.data)
        await store.put(key, attachment.data, content_type=attachment.content_type, sha256=digest)
        document = await finalize_document_intake(
            session,
            context,
            declaration=IntakeDeclaration(
                stream_id=stream.id,
                stream_config=pins.config,
                source_channel=SourceChannel.EMAIL,
                filename=attachment.filename,
                content_type=attachment.content_type,
                sha256=digest,
                size_bytes=len(attachment.data),
                object_key=key,
                source_metadata={
                    "sender": email.sender,
                    "subject": email.subject,
                    "message_id": email.message_id or "",
                },
                document_id=document_id,
                stream_version_id=stream.active_version_id,
                config_fingerprint=pins.config_fingerprint,
            ),
            data=attachment.data,
            scanner=scanner,
            actor_id="system:email-intake",
            actor_type=ActorType.SYSTEM,
        )
        outcomes.append(
            AttachmentOutcome(
                filename=attachment.filename,
                outcome="ingested",
                document_id=str(document.id),
                state=document.state,
                detail=document.state_reason,
            )
        )
    return EmailIntakeReport(outcome="processed", attachments=outcomes)


@dataclass
class MailpitPoller:
    """Local development adapter over Mailpit's REST API: drain unread
    messages as raw MIME, deleting each after a successful hand-off."""

    base_url: str

    async def drain(self) -> list[bytes]:
        import httpx

        raw_messages: list[bytes] = []
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            listing = await client.get("/api/v1/messages")
            listing.raise_for_status()
            for entry in listing.json().get("messages", []):
                message_id = entry["ID"]
                raw = await client.get(f"/api/v1/message/{message_id}/raw")
                raw.raise_for_status()
                raw_messages.append(raw.content)
                await client.request("DELETE", "/api/v1/messages", json={"IDs": [message_id]})
        return raw_messages
