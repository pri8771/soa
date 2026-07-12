import pytest

from soa_worker.registry import HandlerRegistry, JobEnvelope


def test_register_and_resolve() -> None:
    registry = HandlerRegistry()

    @registry.register("document.process")
    async def handle(job: JobEnvelope) -> None:
        pass

    assert registry.resolve("document.process") is handle
    assert registry.registered_types == ["document.process"]


def test_duplicate_registration_is_rejected() -> None:
    registry = HandlerRegistry()

    @registry.register("document.process")
    async def first(job: JobEnvelope) -> None:
        pass

    with pytest.raises(ValueError, match="already registered"):

        @registry.register("document.process")
        async def second(job: JobEnvelope) -> None:
            pass


def test_resolving_unknown_type_raises() -> None:
    registry = HandlerRegistry()
    with pytest.raises(KeyError, match="no handler registered"):
        registry.resolve("unknown.type")
