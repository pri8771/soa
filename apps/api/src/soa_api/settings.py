"""API settings — extends the shared service settings base.

Values load from ``SOA_API_``-prefixed environment variables. Production
safety rules (no debug, no dev secrets/credentials) live in ``soa_config``;
API-specific rules (auth configuration) are enforced here.
"""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from soa_config import DEV_SECRET_KEY, Environment, WebServiceSettings

__all__ = ["ApiSettings", "Environment", "load_settings"]


class ApiSettings(WebServiceSettings):
    model_config = SettingsConfigDict(env_prefix="SOA_API_", frozen=True)

    service_name: str = "soa-api"
    port: int = Field(default=8000, ge=1, le=65535)

    # Authentication (ADR-010): development identity is local-only; OIDC is
    # the production boundary and must be fully configured there.
    auth_dev_mode: bool = True
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None

    # Object storage (STO-002/004). ``storage_backend`` selects the adapter:
    # "s3" (MinIO/S3-compatible, the default) or "gcs" (Google Cloud
    # Storage, the GCP deployment — OPEN-001). When neither is configured
    # the API falls back to an in-process memory store outside production;
    # in production storage must be configured explicitly.
    storage_backend: Literal["s3", "gcs", "filesystem"] = "s3"
    # Filesystem backend (storage_backend="filesystem", development only):
    # object bytes live under ``storage_filesystem_root`` and signed URLs
    # point at the API's own ``/_local-blobs`` endpoint so a browser can
    # upload without MinIO/S3. Never valid in production.
    storage_filesystem_root: str = ".local-storage"
    storage_local_base_url: str = "http://127.0.0.1:8000/_local-blobs"
    storage_endpoint_url: str | None = None
    storage_access_key: str | None = None
    storage_secret_key: str | None = None
    storage_bucket: str = "soa-artifacts"
    storage_region: str = "us-east-1"
    # MinIO needs path-style; AWS accepts both. Encryption: unset uses the
    # provider default; "AES256" or "aws:kms" (with optional key) force it.
    storage_force_path_style: bool = True
    storage_sse: str | None = None
    storage_sse_kms_key_id: str | None = None
    # GCS backend (storage_backend="gcs"): the project owning the bucket,
    # and an optional customer-managed encryption key (a full
    # projects/.../cryptoKeys/... resource).
    storage_gcs_project: str | None = None
    storage_gcs_kms_key_name: str | None = None
    # Signed download URLs are short-lived by design.
    download_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    max_active_data_exports: int = Field(default=3, ge=1, le=100)

    # Malware scanning (ING-004): a real scanner is mandatory outside
    # development/test — the no-op scanner cannot reach production.
    clamav_host: str | None = None
    clamav_port: int = Field(default=3310, ge=1, le=65535)

    # Upload intake policy (ING-002). Plan/stream-level configurability
    # arrives with ING-005; these are the platform maxima.
    upload_session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    max_upload_bytes: int = Field(default=52_428_800, ge=1)  # 50 MiB
    max_pending_upload_sessions: int = Field(default=100, ge=1)
    # Resource-limit platform maxima (ING-005). Streams may configure
    # LOWER values via resolved configuration; never higher.
    max_pages_per_document: int = Field(default=50, ge=1)
    max_total_pixels: int = Field(default=100_000_000, ge=1)  # 100 MP raster
    max_decompressed_bytes: int = Field(default=262_144_000, ge=1)  # 250 MiB
    max_conversion_seconds: int = Field(default=120, ge=1)
    # Public API ingestion (ING-013): per-credential sliding-window limit.
    api_ingest_rate_per_minute: int = Field(default=60, ge=1, le=10_000)
    # Unlike browser uploads (which go directly to signed object storage),
    # multipart API uploads transit the reference Cloud Run HTTP/1 service.
    # Keep the file below its 32 MiB request ceiling with envelope headroom.
    public_ingest_max_file_bytes: int = Field(default=30 * 1024 * 1024, ge=1)
    # Email intake (ING-014): the webhook only accepts requests carrying
    # this shared secret; unset disables the endpoint entirely.
    email_intake_secret: SecretStr | None = None
    # Raw MIME and decoded attachment bounds are enforced before parsing or
    # tenant lookup, preventing unauthenticated memory/CPU amplification. The
    # reference Cloud Run service uses HTTP/1 and therefore keeps raw MIME
    # below its 32 MiB request ceiling. Decoded attachments are lower because
    # MIME base64 expands their wire representation by roughly one third.
    email_intake_max_body_bytes: int = Field(default=30 * 1024 * 1024, ge=1)
    email_intake_max_attachments: int = Field(default=25, ge=1, le=100)
    email_intake_max_total_attachment_bytes: int = Field(default=20 * 1024 * 1024, ge=1)

    # Abuse controls (SEC-003): per-minute sliding-window limits, keyed
    # per principal (or per client IP for identity resolution). These
    # are platform maxima against floods, not billing quotas (ANA-009).
    rate_limit_uploads_per_minute: int = Field(default=60, ge=1, le=10_000)
    rate_limit_download_urls_per_minute: int = Field(default=240, ge=1, le=10_000)
    rate_limit_reprocess_per_minute: int = Field(default=30, ge=1, le=10_000)
    rate_limit_replays_per_minute: int = Field(default=30, ge=1, le=10_000)
    rate_limit_identity_per_minute: int = Field(default=240, ge=1, le=10_000)
    rate_limit_public_auth_per_minute: int = Field(default=240, ge=1, le=10_000)
    rate_limit_email_intake_per_minute: int = Field(default=120, ge=1, le=10_000)
    rate_limit_cleanup_batch_size: int = Field(default=100, ge=1, le=1000)

    # Cross-origin access (SEC-002): a STRICT allowlist of browser
    # origins. Empty means no cross-origin access at all. Wildcards are
    # refused — credentialed wildcard CORS hands the API to every site.
    cors_allowed_origins: tuple[str, ...] = ()
    # The API performs live connection tests and activation probes, so it
    # must enforce the same exact-host outbound policy as worker delivery.
    outbound_destination_allowlist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_cors_allowlist(self) -> Self:
        for origin in self.cors_allowed_origins:
            if "*" in origin:
                raise ValueError(
                    "cors_allowed_origins must name each origin explicitly — "
                    "wildcard credentialed CORS is forbidden"
                )
            if not origin.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValueError(
                    f"CORS origin {origin!r} must be https:// (or localhost for development)"
                )
        return self

    @model_validator(mode="after")
    def _validate_outbound_destination_allowlist(self) -> Self:
        for host in self.outbound_destination_allowlist:
            if not host.strip() or "*" in host or "://" in host or "/" in host:
                raise ValueError("outbound_destination_allowlist must contain exact hostnames only")
        return self

    @model_validator(mode="after")
    def _validate_auth_configuration(self) -> Self:
        oidc_values = (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        if any(oidc_values) and not all(oidc_values):
            raise ValueError(
                "oidc_issuer, oidc_audience, and oidc_jwks_url must be configured together"
            )
        if not self.is_development_like and (
            self.secret_key.get_secret_value() == DEV_SECRET_KEY
            or len(self.secret_key.get_secret_value()) < 32
        ):
            raise ValueError(
                "staging/production secret_key must be non-default and at least 32 characters "
                "because it keys privacy-safe rate-limit identity digests"
            )
        if self.is_production:
            problems: list[str] = []
            if self.auth_dev_mode:
                problems.append("auth_dev_mode must be disabled in production")
            if not self.outbound_destination_allowlist:
                problems.append("production requires an outbound_destination_allowlist")
            if not (self.oidc_issuer and self.oidc_audience and self.oidc_jwks_url):
                problems.append("production requires oidc_issuer, oidc_audience, and oidc_jwks_url")
            elif not (
                self.oidc_issuer.startswith("https://")
                and self.oidc_jwks_url.startswith("https://")
            ):
                problems.append("production OIDC issuer and JWKS URLs must use HTTPS")
            if self.storage_backend == "filesystem":
                problems.append(
                    "storage_backend='filesystem' is development-only and cannot run in production"
                )
            elif self.storage_backend == "gcs":
                if not self.storage_gcs_project:
                    problems.append(
                        "production with storage_backend='gcs' requires storage_gcs_project"
                    )
            elif not (
                self.storage_endpoint_url and self.storage_access_key and self.storage_secret_key
            ):
                problems.append(
                    "production requires storage_endpoint_url, storage_access_key, "
                    "and storage_secret_key"
                )
            if not self.clamav_host:
                problems.append(
                    "production requires clamav_host — unscanned files cannot proceed, "
                    "and the no-op scanner is development-only"
                )
            if (
                self.email_intake_secret is None
                or len(self.email_intake_secret.get_secret_value()) < 32
            ):
                problems.append("production requires an email_intake_secret of 32+ characters")
            if problems:
                raise ValueError("; ".join(problems))
        return self


def load_settings() -> ApiSettings:
    """Load and validate settings from the environment."""
    return ApiSettings()
