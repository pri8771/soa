"""Shared sandbox launch profile for untrusted-content children (SEC-004).

Every process that parses hostile bytes — the page renderer
(PRC-004), the native-text extractor (AIO-002), the OCR engine
(AIO-004) — launches through this profile. It has a parent half and a
child half:

**Parent half** (:func:`child_environment`, :func:`kill_process_tree`):

- the child's environment is built from an explicit passthrough list —
  database URLs, storage keys, provider credentials, and every other
  worker setting are WITHHELD, so an exploited parser finds no secrets
  in its own environment;
- ``TMPDIR`` and ``HOME`` point INSIDE the per-run scratch directory,
  which the parent deletes afterwards — nothing a child writes
  outlives the run or lands outside its jail;
- children start in their own session (``start_new_session=True``) so
  a wall-clock timeout kills the ENTIRE process group, not just the
  direct child.

**Child half** (:func:`set_resource_limits`,
:func:`lock_down_python_child`): rlimits capping CPU seconds, address
space, written-file size, open files, core dumps (a core of untrusted
document memory is a data leak), and — for python children — process
creation, plus python-level denial of sockets, fork/exec, and
subprocess.

Honest scope: the python-level denials stop Python-level attacks; the
rlimits bind native code too, but a native-code exploit is only fully
contained by the CONTAINER layer (non-root, read-only rootfs, seccomp,
``cap_drop: ALL``, pids limit) documented in
``docs/SANDBOX_PROFILE.md``. ``RLIMIT_NPROC`` in particular is not
enforced for root — one more reason the container profile mandates a
non-root user.
"""

import contextlib
import os
import resource
import signal
import sys
from asyncio.subprocess import Process
from collections.abc import Callable

#: The ONLY environment variables that may pass from the worker into a
#: sandbox child. Everything else is withheld.
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "TESSDATA_PREFIX")

#: Open-file budget for a child: input + per-page outputs + library
#: loading, with slack. Far below typical inherited limits.
MAX_OPEN_FILES = 128

# macOS exposes RLIMIT_AS but rejects attempts to lower it for a running
# Python process (``ValueError: current limit exceeds maximum limit``).
# Linux — the production container target — supports and enforces it.
# Keep this capability explicit so local development does not crash every
# parser child while production never silently loses the address-space cap.
ADDRESS_SPACE_LIMIT_SUPPORTED = sys.platform != "darwin"


def child_environment(tmpdir: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for a sandbox child: the passthrough list,
    temp/home confined to ``tmpdir``, and the caller's ``extra``."""
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    env["TMPDIR"] = tmpdir
    env["HOME"] = tmpdir
    if extra:
        env.update(extra)
    return env


def python_child_environment(tmpdir: str) -> dict[str, str]:
    """:func:`child_environment` plus the parent's import path, so
    ``python -m soa_worker.*_sandbox`` children resolve their module."""
    return child_environment(tmpdir, extra={"PYTHONPATH": os.pathsep.join(sys.path)})


def kill_process_tree(process: Process) -> None:
    """Kill a timed-out child and everything it spawned. The child is a
    session leader (``start_new_session=True``), so its process group
    id is its pid and the group signal reaches any grandchildren."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def set_resource_limits(cpu_seconds: int, memory_bytes: int) -> None:
    """rlimits every sandbox child gets (safe as a ``preexec_fn``):
    CPU seconds, address space, no core dumps, single written file
    capped at the memory budget, and a small open-file budget."""
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    if ADDRESS_SPACE_LIMIT_SUPPORTED:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (memory_bytes, memory_bytes))
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    open_files = MAX_OPEN_FILES if hard == resource.RLIM_INFINITY else min(MAX_OPEN_FILES, hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))


def lock_down_python_child(cpu_seconds: int, memory_bytes: int) -> None:
    """Full lockdown for a python sandbox child — called by the child
    itself before it touches any parser."""
    set_resource_limits(cpu_seconds, memory_bytes)
    # A renderer/parser never forks; deny process creation outright.
    # (Root bypasses RLIMIT_NPROC — the container profile mandates
    # non-root precisely so limits like this one hold.)
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    _disable_network()
    _disable_process_creation()


def _denied(what: str) -> Callable[..., None]:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(f"{what} is disabled inside the sandbox")

    return _refuse


def _disable_network() -> None:
    import socket

    for name in ("socket", "socketpair", "create_connection", "fromfd", "getaddrinfo"):
        setattr(socket, name, _denied("network access"))


def _disable_process_creation() -> None:
    import subprocess

    for name in [attr for attr in dir(os) if attr.startswith(("fork", "exec", "spawn", "popen"))]:
        setattr(os, name, _denied("process creation"))
    os.system = _denied("process creation")  # type: ignore[assignment]
    os.posix_spawn = _denied("process creation")  # type: ignore[assignment]
    os.posix_spawnp = _denied("process creation")  # type: ignore[assignment]
    subprocess.Popen = _denied("process creation")  # type: ignore[misc,assignment]
    subprocess.run = _denied("process creation")  # type: ignore[assignment]


__all__ = [
    "ADDRESS_SPACE_LIMIT_SUPPORTED",
    "ENV_PASSTHROUGH",
    "MAX_OPEN_FILES",
    "child_environment",
    "kill_process_tree",
    "lock_down_python_child",
    "python_child_environment",
    "set_resource_limits",
]
