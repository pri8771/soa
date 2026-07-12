#!/usr/bin/env python3
"""Validate repository documentation.

Checks, with no third-party dependencies:

1. Every repository-relative markdown link points at a file that exists.
2. Every ``make <target>`` command mentioned in markdown *code* (fenced
   blocks and inline code spans — prose is ignored) exists in the Makefile,
   so documented commands cannot silently drift from real ones.

Exits non-zero listing every problem found. Run locally or in CI:
``python scripts/check_docs.py``
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
MAKE_PATTERN = re.compile(r"\bmake ([a-z][a-z0-9-]*)\b")
MAKE_TARGET_PATTERN = re.compile(r"^([a-zA-Z0-9_-]+):", re.MULTILINE)

SKIP_DIRS = {".git", "node_modules", ".venv", "dist", ".pytest_cache", ".ruff_cache"}


def markdown_files() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*.md"):
        if not any(part in SKIP_DIRS for part in path.parts):
            files.append(path)
    return sorted(files)


def check_links(files: list[Path]) -> list[str]:
    problems = []
    for md_file in files:
        text = md_file.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (md_file.parent / path_part).resolve()
            if not resolved.exists():
                rel = md_file.relative_to(REPO_ROOT)
                problems.append(f"{rel}: broken link -> {target}")
    return problems


FENCED_BLOCK_PATTERN = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")


def code_regions(text: str) -> str:
    """Concatenate fenced code blocks and inline code spans."""
    blocks = FENCED_BLOCK_PATTERN.findall(text)
    without_blocks = FENCED_BLOCK_PATTERN.sub("", text)
    spans = INLINE_CODE_PATTERN.findall(without_blocks)
    return "\n".join(blocks + spans)


def check_make_commands(files: list[Path]) -> list[str]:
    makefile = REPO_ROOT / "Makefile"
    if not makefile.exists():
        return ["Makefile is missing but documentation references make commands"]
    targets = set(MAKE_TARGET_PATTERN.findall(makefile.read_text(encoding="utf-8")))
    problems = []
    for md_file in files:
        code = code_regions(md_file.read_text(encoding="utf-8"))
        for match in MAKE_PATTERN.finditer(code):
            target = match.group(1)
            if target not in targets:
                rel = md_file.relative_to(REPO_ROOT)
                problems.append(f"{rel}: documented command 'make {target}' has no Makefile target")
    return problems


def main() -> int:
    files = markdown_files()
    problems = check_links(files) + check_make_commands(files)
    if problems:
        print(f"Documentation validation failed ({len(problems)} problems):")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"Documentation OK: {len(files)} markdown files checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
