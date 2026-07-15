import os
import subprocess
import sys
from pathlib import Path


def run_dev_script(*arguments: str, environment: dict[str, str] | None = None) -> str:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "dev.py"), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout


def test_dev_runner_supervises_every_application() -> None:
    output = run_dev_script("--print-commands")
    assert {line.split(":", 1)[0] for line in output.splitlines()} == {
        "api",
        "worker",
        "web",
    }


def test_dev_runner_supplies_no_docker_defaults() -> None:
    environment = os.environ.copy()
    for key in (
        "SOA_API_STORAGE_BACKEND",
        "SOA_API_CORS_ALLOWED_ORIGINS",
        "SOA_WORKER_STORAGE_BACKEND",
        "VITE_API_BASE_URL",
    ):
        environment.pop(key, None)

    output = run_dev_script("--print-environment", environment=environment)
    assert "SOA_API_STORAGE_BACKEND=filesystem" in output
    assert 'SOA_API_CORS_ALLOWED_ORIGINS=["http://localhost:5173"]' in output
    assert "SOA_WORKER_STORAGE_BACKEND=filesystem" in output
    assert "VITE_API_BASE_URL=http://127.0.0.1:8000" in output


def test_dev_runner_preserves_explicit_operator_overrides() -> None:
    environment = os.environ | {"SOA_WORKER_STORAGE_BACKEND": "gcs"}
    output = run_dev_script("--print-environment", environment=environment)
    worker_line = next(line for line in output.splitlines() if line.startswith("worker:"))
    assert "SOA_WORKER_STORAGE_BACKEND=gcs" in worker_line
