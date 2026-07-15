"""Sandbox hardening tests (SEC-004): the launch profile keeps worker
secrets out of child environments, confines temp/home to the per-run
scratch dir, the child lockdown denies network/fork/exec and pins
resource limits, and a wall-clock timeout kills the WHOLE process
group — grandchildren included."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import soa_worker.rendering as rendering
from soa_worker.rendering import RenderError, RenderFailure, RenderLimits, render_document
from soa_worker.sandbox import (
    ADDRESS_SPACE_LIMIT_SUPPORTED,
    ENV_PASSTHROUGH,
    MAX_OPEN_FILES,
    child_environment,
    python_child_environment,
)

MINIMAL_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >> endobj
trailer << /Root 1 0 R >>
"""


class TestChildEnvironment:
    def test_worker_settings_and_secrets_are_withheld(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOA_WORKER_DATABASE_URL", "postgresql://secret")
        monkeypatch.setenv("SOA_WORKER_STORAGE_SECRET_KEY", "s3-secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = child_environment("/scratch/tmp")
        assert set(env) <= set(ENV_PASSTHROUGH) | {"TMPDIR", "HOME"}
        assert "secret" not in str(env)
        assert env["PATH"] == "/usr/bin"
        assert env["TMPDIR"] == "/scratch/tmp"
        assert env["HOME"] == "/scratch/tmp"

    def test_python_children_get_the_import_path_and_nothing_more(self) -> None:
        env = python_child_environment("/scratch/tmp")
        assert env["PYTHONPATH"] == os.pathsep.join(sys.path)
        assert set(env) <= set(ENV_PASSTHROUGH) | {"TMPDIR", "HOME", "PYTHONPATH"}


async def test_render_children_launch_with_no_secrets_and_confined_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A probe stands in for the sandbox and asserts, FROM INSIDE, what
    # the launch profile promised. Any failed assertion exits non-zero,
    # which the parent classifies as a crash — so success proves it.
    monkeypatch.setenv("SOA_WORKER_CANARY_SECRET", "must-not-leak")
    probe = (
        "import json, os\n"
        "leaked = [k for k in os.environ if 'SOA_' in k or 'CANARY' in k]\n"
        "assert not leaked, f'leaked into the sandbox: {leaked}'\n"
        "tmp = os.environ['TMPDIR']\n"
        "assert os.environ['HOME'] == tmp\n"
        "assert os.access(tmp, os.W_OK), 'the confined temp dir must be writable'\n"
        "workdir = os.path.realpath(os.path.dirname(os.path.normpath(tmp)))\n"
        "assert workdir == os.path.realpath(os.getcwd()), 'cwd must be the scratch dir'\n"
        "print(json.dumps({'pages': []}))\n"
    )
    monkeypatch.setattr(rendering, "_SANDBOX_ARGV", [sys.executable, "-c", probe])
    assert await render_document(MINIMAL_PDF, content_type="application/pdf") == []


async def test_lockdown_denies_network_processes_and_pins_limits() -> None:
    # The lockdown must run in a throwaway process — it is irreversible
    # by design, so it cannot be exercised in the test process itself.
    memory = 1_073_741_824
    probe = f"""
import resource, socket, subprocess, os
from soa_worker.sandbox import lock_down_python_child

lock_down_python_child(30, {memory})

for attempt in (
    lambda: socket.socket(),
    lambda: socket.create_connection(("127.0.0.1", 9)),
    lambda: os.fork(),
    lambda: os.execv("/bin/true", ["/bin/true"]),
    lambda: os.system("true"),
    lambda: subprocess.run(["true"]),
):
    try:
        attempt()
        raise SystemExit("ESCAPED: " + repr(attempt))
    except PermissionError:
        pass

assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
assert resource.getrlimit(resource.RLIMIT_FSIZE) == ({memory}, {memory})
assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= {MAX_OPEN_FILES}
if {ADDRESS_SPACE_LIMIT_SUPPORTED!r}:
    assert resource.getrlimit(resource.RLIMIT_AS) == ({memory}, {memory})
    try:
        hog = bytearray({memory} * 2)
        raise SystemExit("ESCAPED: allocated past the address-space limit")
    except MemoryError:
        pass

print("LOCKED")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "LOCKED"


async def test_timeout_kills_the_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hostile child that spawns a grandchild and hangs: the parent's
    # wall-clock kill must take out BOTH — an orphaned grandchild would
    # keep burning CPU after the run was declared dead.
    pid_file = tmp_path / "grandchild.pid"
    probe = (
        "import subprocess, sys, time\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(grandchild.pid))\n"
        "time.sleep(600)\n"
    )
    monkeypatch.setattr(rendering, "_SANDBOX_ARGV", [sys.executable, "-c", probe])
    with pytest.raises(RenderError) as error:
        await render_document(
            MINIMAL_PDF,
            content_type="application/pdf",
            limits=RenderLimits(timeout_seconds=2.0),
        )
    assert error.value.failure is RenderFailure.TIMEOUT

    grandchild_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _alive(grandchild_pid):
            return
        time.sleep(0.1)
    os.kill(grandchild_pid, 9)  # do not leak it beyond the test
    pytest.fail("the grandchild survived the process-group kill")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # a reaped-in-progress zombie counts as dead
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    return True
