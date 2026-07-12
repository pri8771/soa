"""ASGI entry point: ``uvicorn soa_api.main:app``."""

from soa_api.app import create_app
from soa_api.settings import load_settings
from soa_config.logging import configure_logging

_settings = load_settings()
configure_logging(
    service_name=_settings.service_name,
    environment=_settings.environment.value,
    level="DEBUG" if _settings.debug else "INFO",
)
app = create_app(_settings)
