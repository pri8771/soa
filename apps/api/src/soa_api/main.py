"""ASGI entry point: ``uvicorn soa_api.main:app``."""

from soa_api.app import create_app

app = create_app()
