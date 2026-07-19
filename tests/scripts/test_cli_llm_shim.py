"""Tests for scripts/cli_llm_shim.py (dev-only CLI-subscription LLM shim).

Never invokes a real CLI: every subprocess boundary is monkeypatched.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "cli_llm_shim", REPO_ROOT / "scripts" / "cli_llm_shim.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shim() -> ModuleType:
    return _load_module()


@pytest.fixture
def client(shim: ModuleType) -> TestClient:
    return TestClient(shim.create_app())


# --- message flattening -----------------------------------------------------


def test_flatten_messages_prefixes_each_role_block(shim: ModuleType) -> None:
    prompt = shim.flatten_messages(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Reply with exactly: pong"},
        ]
    )
    assert prompt == "[system]\nBe terse.\n\n[user]\nReply with exactly: pong"


def test_flatten_messages_handles_list_content_parts(shim: ModuleType) -> None:
    prompt = shim.flatten_messages(
        [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    )
    assert prompt == "[user]\nhello"


def test_flatten_messages_defaults_missing_role_to_user(shim: ModuleType) -> None:
    prompt = shim.flatten_messages([{"content": "hi"}])
    assert prompt == "[user]\nhi"


# --- claude JSON parsing -----------------------------------------------------


def test_parse_claude_output_extracts_result_field(shim: ModuleType) -> None:
    assert shim.parse_claude_output('{"result": "pong"}') == "pong"


def test_parse_claude_output_rejects_invalid_json(shim: ModuleType) -> None:
    with pytest.raises(shim.ShimError) as excinfo:
        shim.parse_claude_output("not json")
    assert excinfo.value.status_code == 502


def test_parse_claude_output_rejects_missing_result(shim: ModuleType) -> None:
    with pytest.raises(shim.ShimError) as excinfo:
        shim.parse_claude_output('{"other": "field"}')
    assert excinfo.value.status_code == 502


# --- codex JSONL parsing -----------------------------------------------------


def test_parse_codex_jsonl_keeps_the_last_agent_message(shim: ModuleType) -> None:
    output = "\n".join(
        [
            '{"type": "agent_message", "message": "first"}',
            '{"type": "other_event"}',
            '{"type": "agent_message", "message": "pong"}',
        ]
    )
    assert shim.parse_codex_jsonl(output) == "pong"


def test_parse_codex_jsonl_rejects_no_agent_message(shim: ModuleType) -> None:
    with pytest.raises(shim.ShimError) as excinfo:
        shim.parse_codex_jsonl('{"type": "other_event"}')
    assert excinfo.value.status_code == 502


def test_parse_codex_jsonl_skips_malformed_lines(shim: ModuleType) -> None:
    output = "\n".join(["not json", '{"type": "agent_message", "message": "pong"}'])
    assert shim.parse_codex_jsonl(output) == "pong"


# --- subprocess boundary (never invokes a real CLI) --------------------------


def test_run_claude_returns_503_when_cli_missing(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    with pytest.raises(shim.ShimError) as excinfo:
        shim.run_claude("hi", timeout_seconds=5)
    assert excinfo.value.status_code == 503


def test_run_claude_returns_502_on_timeout(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    with pytest.raises(shim.ShimError) as excinfo:
        shim.run_claude("hi", timeout_seconds=5)
    assert excinfo.value.status_code == 502


def test_run_claude_returns_502_on_nonzero_exit_with_truncated_stderr(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="x" * 1000
        )

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    with pytest.raises(shim.ShimError) as excinfo:
        shim.run_claude("hi", timeout_seconds=5)
    assert excinfo.value.status_code == 502
    assert len(excinfo.value.message) < 1000


def test_run_claude_happy_path(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout='{"result": "pong"}', stderr=""
        )

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    assert shim.run_claude("hi", timeout_seconds=5) == "pong"


def test_run_codex_returns_503_when_cli_missing(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("codex")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    with pytest.raises(shim.ShimError) as excinfo:
        shim.run_codex("hi", timeout_seconds=5)
    assert excinfo.value.status_code == 503


# --- HTTP surface -------------------------------------------------------------


def test_list_models_is_static(client: TestClient) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 200
    ids = {entry["id"] for entry in response.json()["data"]}
    assert ids == {"claude-cli", "codex-cli"}


def test_chat_completions_routes_claude_models_to_claude(
    shim: ModuleType, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shim, "run_claude", lambda prompt, *, timeout_seconds: "pong")
    response = client.post(
        "/v1/chat/completions",
        json={"model": "claude-cli", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["model"] == "claude-cli"
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_chat_completions_routes_codex_models_to_codex(
    shim: ModuleType, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shim, "run_codex", lambda prompt, *, timeout_seconds: "pong")
    response = client.post(
        "/v1/chat/completions",
        json={"model": "codex-cli", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "pong"


def test_chat_completions_returns_503_when_codex_binary_absent(
    shim: ModuleType, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("codex")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "codex-cli", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503


def test_chat_completions_rejects_unknown_model_family(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400


def test_shim_timeout_seconds_defaults_and_reads_env(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOA_SHIM_TIMEOUT_SECONDS", raising=False)
    assert shim.shim_timeout_seconds() == shim.DEFAULT_TIMEOUT_SECONDS
    monkeypatch.setenv("SOA_SHIM_TIMEOUT_SECONDS", "45")
    assert shim.shim_timeout_seconds() == 45.0
