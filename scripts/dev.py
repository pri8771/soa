#!/usr/bin/env python3
"""Run the local web, API, and durable worker as one supervised process group."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


@dataclass(frozen=True)
class Service:
    name: str
    command: tuple[str, ...]
    environment_defaults: tuple[tuple[str, str], ...] = ()


SERVICES = (
    Service(
        "api",
        ("uv", "run", "uvicorn", "soa_api.main:app", "--reload"),
        (
            ("SOA_API_STORAGE_BACKEND", "filesystem"),
            ("SOA_API_CORS_ALLOWED_ORIGINS", '["http://localhost:5173"]'),
        ),
    ),
    Service(
        "worker",
        ("uv", "run", "soa-worker"),
        (("SOA_WORKER_STORAGE_BACKEND", "filesystem"),),
    ),
    Service(
        "web",
        ("pnpm", "--filter", "@soa/web", "run", "dev"),
        (("VITE_API_BASE_URL", "http://127.0.0.1:8000"),),
    ),
)


def _dotenv_values(path: Path) -> dict[str, str]:
    """Read the small POSIX-style subset used by the repository template."""

    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def service_environment(service: Service) -> dict[str, str]:
    """Return a child environment with runnable, overrideable local defaults.

    Explicit shell values always win. The defaults make ``make dev`` match the
    no-Docker local-development contract even when a stale ``.env`` still
    selects MinIO.
    """

    environment = _dotenv_values(ENV_FILE)
    environment.update(os.environ)
    for key, value in service.environment_defaults:
        # ``make dev`` is intentionally the zero-container profile. A shell
        # export can select another backend; a copied historical .env cannot
        # silently turn MinIO back into a prerequisite.
        if key not in os.environ:
            environment[key] = value
    return environment


def stop_processes(processes: list[tuple[Service, subprocess.Popen[bytes]]]) -> None:
    """Terminate children, then kill only those that ignore the grace period."""
    for _, process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 8
    for _, process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        if process.poll() is None:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
    for _, process in processes:
        if process.poll() is None:
            process.wait()


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="print the supervised commands without starting them",
    )
    parser.add_argument(
        "--print-environment",
        action="store_true",
        help="print only the non-secret local defaults without starting services",
    )
    args = parser.parse_args()
    if args.print_commands:
        for service in SERVICES:
            print(f"{service.name}: {' '.join(service.command)}")
        return 0
    if args.print_environment:
        for service in SERVICES:
            environment = service_environment(service)
            defaults = " ".join(
                f"{key}={environment[key]}" for key, _ in service.environment_defaults
            )
            print(f"{service.name}: {defaults}")
        return 0

    processes: list[tuple[Service, subprocess.Popen[bytes]]] = []
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for service in SERVICES:
            print(f"[dev] starting {service.name}: {' '.join(service.command)}", flush=True)
            processes.append(
                (
                    service,
                    subprocess.Popen(
                        service.command,
                        cwd=ROOT,
                        env=service_environment(service),
                    ),
                )
            )
        while not stopping:
            for service, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    print(
                        f"[dev] {service.name} exited with status {return_code}; "
                        "stopping all services",
                        file=sys.stderr,
                    )
                    return return_code if return_code != 0 else 1
            time.sleep(0.2)
        return 0
    except FileNotFoundError as error:
        print(f"[dev] required executable not found: {error.filename}", file=sys.stderr)
        return 127
    finally:
        stop_processes(processes)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(run())
