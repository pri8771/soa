"""Worker container liveness probe tests (REL-001).

The probe reads the heartbeat file the run loop maintains and decides
healthy/stale/missing; the run loop actually writes that file when a
liveness path is configured.
"""

import asyncio
from pathlib import Path

from soa_worker.healthcheck import check
from soa_worker.registry import HandlerRegistry
from soa_worker.settings import Environment, WorkerSettings
from soa_worker.worker import Worker, WorkerState


def _settings(tmp_path: Path, **overrides: object) -> WorkerSettings:
    return WorkerSettings(
        environment=Environment.TEST,
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.01,
        liveness_file=str(tmp_path / "beat"),
        **overrides,
    )


def test_disabled_when_no_file_configured() -> None:
    settings = WorkerSettings(environment=Environment.TEST)
    healthy, message = check(settings)
    assert healthy is True
    assert "disabled" in message


def test_missing_file_is_unhealthy(tmp_path: Path) -> None:
    healthy, message = check(_settings(tmp_path))
    assert healthy is False
    assert "does not exist" in message


def test_fresh_file_is_healthy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    Path(settings.liveness_file or "").write_text("1000.0\n", encoding="utf-8")
    beat = Path(settings.liveness_file or "").stat().st_mtime
    healthy, _ = check(settings, now=beat + 0.005)
    assert healthy is True


def test_stale_file_is_unhealthy(tmp_path: Path) -> None:
    # heartbeat 0.01s times factor 3 = 0.03s tolerance; 10s old is stale.
    settings = _settings(tmp_path, liveness_staleness_factor=3.0)
    path = Path(settings.liveness_file or "")
    path.write_text("1000.0\n", encoding="utf-8")
    healthy, message = check(settings, now=path.stat().st_mtime + 10.0)
    assert healthy is False
    assert "stalled" in message


async def test_run_loop_writes_the_liveness_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    beat_file = Path(settings.liveness_file or "")
    worker = Worker(settings, HandlerRegistry())
    task = asyncio.create_task(worker.run())
    deadline = asyncio.get_event_loop().time() + 2.0
    while not beat_file.exists():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("worker never wrote its liveness file")
        await asyncio.sleep(0.005)
    # And the probe agrees the worker is alive.
    healthy, _ = check(settings)
    assert healthy is True

    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)
    assert worker.state is WorkerState.STOPPED
