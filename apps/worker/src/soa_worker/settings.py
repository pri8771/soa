"""Worker settings — extends the shared service settings base.

Values load from ``SOA_WORKER_``-prefixed environment variables. Production
safety rules (no debug, no dev secrets/credentials) live in ``soa_config``.
"""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from soa_config import BaseServiceSettings, Environment

__all__ = ["Environment", "WorkerSettings", "load_settings"]


class WorkerSettings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="SOA_WORKER_", frozen=True)

    service_name: str = "soa-worker"

    #: Object storage the worker reads originals from and writes derived
    #: artifacts to — must match the API's store. "s3" (MinIO/S3), "gcs",
    #: or "filesystem" (development-only local disk shared with the API).
    storage_backend: Literal["s3", "gcs", "filesystem"] = "s3"
    storage_filesystem_root: str = ".local-storage"
    storage_endpoint_url: str | None = None
    storage_access_key: str | None = None
    storage_secret_key: str | None = None
    storage_bucket: str = "soa-artifacts"
    storage_region: str = "us-east-1"
    storage_force_path_style: bool = True
    storage_sse: str | None = None
    storage_sse_kms_key_id: str | None = None
    storage_gcs_project: str | None = None
    storage_gcs_kms_key_name: str | None = None

    poll_interval_seconds: float = Field(default=1.0, gt=0)
    heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    #: Container liveness (REL-001). When set, the heartbeat loop touches
    #: this file every beat; ``python -m soa_worker.healthcheck`` fails if
    #: it is missing or older than heartbeat_interval times the staleness
    #: factor. Unset disables the file (the default outside containers).
    liveness_file: str | None = None
    liveness_staleness_factor: float = Field(default=3.0, gt=1)

    # Deprecated development fallback retained for environment compatibility.
    # Runtime extraction is selected from each run's pinned provider policy.
    extraction_provider: str = "mock"
    export_destination_allowlist: tuple[str, ...] = ()
    #: HTTPS endpoint that receives transactional domain events. The event UUID
    #: is sent as Idempotency-Key so receivers can absorb retry duplicates.
    outbox_publish_url: str | None = None
    #: Optional local OpenAI-compatible extraction endpoint (AIO-007).
    #: Unset (the default) means the profile never registers. Point this
    #: at Ollama/vLLM/llama.cpp; see docs/LLM_PROVIDERS.md.
    local_llm_endpoint: str | None = None
    local_llm_model: str = "local"

    #: Optional BYO hosted Claude key (AIO-008). When set, the hosted
    #: Claude extraction adapter registers at startup; unset means it
    #: does not exist (fail-closed, AIO-006). The model/endpoint are
    #: overridable for pinning or a proxy.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-4-5"

    #: Optional BYO hosted Gemini key (AIO-009 — native generateContent
    #: adapter). Fail-closed like the others: unset means no such provider.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.0-flash"

    #: Optional BYO hosted OpenAI-compatible key (OpenAI, or Gemini's
    #: OpenAI-compatible endpoint). Requires both a key and an endpoint;
    #: reuses the AIO-007 adapter with Bearer auth. Unset means no such
    #: provider. ``hosted_openai_provider_name`` names it in the registry
    #: so an operator can tell OpenAI from Gemini.
    hosted_openai_api_key: SecretStr | None = None
    hosted_openai_endpoint: str | None = None
    hosted_openai_model: str = "gpt-4o"
    hosted_openai_provider_name: str = "hosted-openai-compatible"
    #: The processing region the hosted OpenAI-compatible provider runs
    #: in — declared honestly so tenant data policy is enforced against
    #: the truth (a lowercase region slug, e.g. "us", "eu").
    hosted_openai_region: str = "us"

    @model_validator(mode="after")
    def _validate_runtime_dependencies(self) -> Self:
        if self.is_production:
            problems: list[str] = []
            if self.storage_backend == "filesystem":
                problems.append("filesystem storage is development-only")
            elif self.storage_backend == "gcs":
                if not self.storage_gcs_project:
                    problems.append("GCS storage requires storage_gcs_project")
            elif not (
                self.storage_endpoint_url and self.storage_access_key and self.storage_secret_key
            ):
                problems.append("S3 storage requires endpoint, access key, and secret key")
            if not self.export_destination_allowlist:
                problems.append("production requires an export_destination_allowlist")
            if not self.outbox_publish_url or not self.outbox_publish_url.startswith("https://"):
                problems.append("production requires an HTTPS outbox_publish_url")
            if problems:
                raise ValueError("; ".join(problems))
        return self


def load_settings() -> WorkerSettings:
    """Load and validate settings from the environment."""
    return WorkerSettings()
