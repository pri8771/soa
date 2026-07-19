#!/usr/bin/env python3
"""Dev-only OpenAI-chat-completions shim backed by CLI subscriptions.

**Never a production provider.** SOA's `local-openai-compatible` provider
only needs an endpoint that speaks the OpenAI chat-completions protocol —
that is how Ollama is wired today (`SOA_WORKER_LOCAL_LLM_ENDPOINT`). This
script translates ``POST /v1/chat/completions`` into a ``claude -p`` or
``codex exec`` subprocess call, so a developer with a Claude Code or ChatGPT
subscription (but no API key) can exercise real-model extraction through the
existing provider path with zero key handling.

Run: ``make shim`` or ``uv run python scripts/cli_llm_shim.py``. Binds to
``127.0.0.1`` only; no auth (localhost dev tool). Subscription rate limits
apply. Logs one line per request (model, latency, exit status) — never the
prompt content.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("cli_llm_shim")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_PORT = 8095
DEFAULT_TIMEOUT_SECONDS = 300.0
STDERR_TAIL_CHARS = 500

MODELS = ("claude-cli", "codex-cli")


def shim_timeout_seconds() -> float:
    raw = os.environ.get("SOA_SHIM_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT_SECONDS
    return float(raw)


class ShimError(Exception):
    """A subprocess call failed or returned an unusable shape."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten OpenAI chat ``messages`` into one prompt string, prefixing
    each turn with its role so a plain-text CLI still sees the structure."""
    blocks = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if not isinstance(content, str):
            # Some callers send a list of content parts; join the text ones.
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        blocks.append(f"[{role}]\n{content}")
    return "\n\n".join(blocks)


def parse_claude_output(stdout: str) -> str:
    """``claude -p ... --output-format json`` prints one JSON object whose
    ``result`` field is the assistant's text."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ShimError(502, f"claude CLI returned invalid JSON: {error}") from error
    result = parsed.get("result")
    if not isinstance(result, str):
        raise ShimError(502, "claude CLI JSON had no string 'result' field")
    return result


def parse_codex_jsonl(stdout: str) -> str:
    """``codex exec ... --json`` prints one JSON object per line; the final
    agent-message event carries the assistant's text."""
    last_text: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        msg_type = event.get("type") or event.get("msg", {}).get("type")
        text = None
        if isinstance(event.get("msg"), dict):
            text = event["msg"].get("message") or event["msg"].get("text")
        text = text or event.get("message") or event.get("text")
        if msg_type in ("agent_message", "agent-message") and isinstance(text, str):
            last_text = text
        elif isinstance(text, str) and msg_type is None:
            last_text = text
    if last_text is None:
        raise ShimError(502, "codex CLI JSONL output had no agent message")
    return last_text


def run_claude(prompt: str, *, timeout_seconds: float) -> str:
    try:
        completed = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as error:
        raise ShimError(503, "the 'claude' CLI is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise ShimError(502, f"claude CLI timed out after {timeout_seconds:.0f}s") from error
    if completed.returncode != 0:
        raise ShimError(502, f"claude CLI exited {completed.returncode}: {_tail(completed.stderr)}")
    return parse_claude_output(completed.stdout)


def run_codex(prompt: str, *, timeout_seconds: float) -> str:
    try:
        completed = subprocess.run(
            ["codex", "exec", prompt, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as error:
        raise ShimError(503, "the 'codex' CLI is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise ShimError(502, f"codex CLI timed out after {timeout_seconds:.0f}s") from error
    if completed.returncode != 0:
        raise ShimError(502, f"codex CLI exited {completed.returncode}: {_tail(completed.stderr)}")
    return parse_codex_jsonl(completed.stdout)


def _tail(text: str) -> str:
    return text[-STDERR_TAIL_CHARS:]


def completion_body(*, model: str, content: str) -> dict[str, Any]:
    """Minimal OpenAI-shape response. Zero usage is honest-unknown — the
    CLI subprocess reports no token counts, and SOA treats unknown usage
    as unknown rather than inventing a number."""
    return {
        "id": f"cli-shim-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def create_app() -> FastAPI:
    app = FastAPI(title="SOA CLI LLM shim", docs_url=None, redoc_url=None)

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": model, "object": "model"} for model in MODELS]}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        body = await request.json()
        model = body.get("model", "")
        messages = body.get("messages", [])
        prompt = flatten_messages(messages)
        timeout_seconds = shim_timeout_seconds()

        started = time.monotonic()
        status = "ok"
        try:
            if model.startswith("claude"):
                content = run_claude(prompt, timeout_seconds=timeout_seconds)
            elif model.startswith("codex"):
                content = run_codex(prompt, timeout_seconds=timeout_seconds)
            else:
                status = "error"
                return JSONResponse(
                    status_code=400,
                    content={"error": f"unknown model {model!r}; expected claude-* or codex-*"},
                )
        except ShimError as error:
            status = "error"
            return JSONResponse(status_code=error.status_code, content={"error": error.message})
        finally:
            latency_ms = (time.monotonic() - started) * 1000
            # Never log prompt content — only model, latency, exit status.
            logger.info("model=%s latency_ms=%.0f status=%s", model, latency_ms, status)

        return JSONResponse(content=completion_body(model=model, content=content))

    return app


app = create_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("SOA_SHIM_PORT", str(DEFAULT_PORT)))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
