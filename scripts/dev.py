#!/usr/bin/env python3
"""Run the local web, API, and durable worker as one supervised process group."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Service:
    name: str
    command: tuple[str, ...]


SERVICES = (
    Service("api", ("uv", "run", "uvicorn", "soa_api.main:app", "--reload")),
    Service("worker", ("uv", "run", "soa-worker")),
    Service("web", ("pnpm", "--filter", "@soa/web", "run", "dev")),
)


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
    args = parser.parse_args()
    if args.print_commands:
        for service in SERVICES:
            print(f"{service.name}: {' '.join(service.command)}")
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
            processes.append((service, subprocess.Popen(service.command, cwd=ROOT)))
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
