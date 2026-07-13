"""Worker settings — extends the shared service settings base.

Values load from ``SOA_WORKER_``-prefixed environment variables. Production
safety rules (no debug, no dev secrets/credentials) live in ``soa_config``.
"""

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from soa_config import BaseServiceSettings, Environment

__all__ = ["Environment", "WorkerSettings", "load_settings"]


class WorkerSettings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="SOA_WORKER_", frozen=True)

    service_name: str = "soa-worker"
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    #: Optional local OpenAI-compatible extraction endpoint (AIO-007).
    #: Unset (the default) means the profile never registers.
    local_llm_endpoint: str | None = None
    local_llm_model: str = "local"


def load_settings() -> WorkerSettings:
    """Load and validate settings from the environment."""
    return WorkerSettings()
