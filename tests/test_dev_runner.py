import subprocess
import sys
from pathlib import Path


def test_dev_runner_supervises_every_application() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "dev.py"), "--print-commands"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert {line.split(":", 1)[0] for line in result.stdout.splitlines()} == {
        "api",
        "worker",
        "web",
    }
