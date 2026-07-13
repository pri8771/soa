#!/usr/bin/env python3
"""Supply-chain security gate CLI (SEC-011).

A thin command-line front end over :mod:`soa_config.supply_chain` — the
policy lives in that module (unit-tested); this script wires it to the
scanner JSON files CI produces and turns the decision into an exit code.

Commands::

    python scripts/supply_chain.py check-exceptions
        Validate security/vulnerability-exceptions.toml. Exit 1 on any
        malformed OR expired entry.

    python scripts/supply_chain.py evaluate \
        --pip-audit pip-audit.json --pnpm-audit pnpm-audit.json
        Parse both reports, apply the severity policy minus active
        exceptions, print the decision, exit 1 if anything blocks the
        release (or an exception has expired).

Both accept ``--today YYYY-MM-DD`` (defaults to the system date) so the
expiry logic is testable, and ``--exceptions PATH`` to point at a
non-default registry.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_engine() -> ModuleType:
    """Load the policy engine by path.

    The engine (``soa_config.supply_chain``) is pure standard library,
    but importing it through the package would drag in ``soa_config``'s
    pydantic-backed ``__init__``. Loading the file directly keeps this
    gate runnable with a bare interpreter — no project install needed.
    """
    source = REPO_ROOT / "packages" / "config" / "src" / "soa_config" / "supply_chain.py"
    spec = importlib.util.spec_from_file_location("_soa_supply_chain", source)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load policy engine from {source}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass decoration can resolve the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_engine = _load_engine()
Exception_ = _engine.Exception_
ExceptionError = _engine.ExceptionError
Finding = _engine.Finding
PolicyReport = _engine.PolicyReport
evaluate = _engine.evaluate
load_exceptions = _engine.load_exceptions
parse_pip_audit = _engine.parse_pip_audit
parse_pnpm_audit = _engine.parse_pnpm_audit

DEFAULT_EXCEPTIONS = REPO_ROOT / "security" / "vulnerability-exceptions.toml"


def _today(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(UTC).date()


def _load_report(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _print_exception(exc: Exception_) -> None:
    print(f"    {exc.ecosystem}:{exc.id} ({exc.package}) — {exc.owner}, expires {exc.expires}")


def _print_finding(finding: Finding) -> None:
    fix = "fix available" if finding.fix_available else "NO fix released"
    print(
        f"    {finding.ecosystem}:{finding.vulnerability_id} "
        f"[{finding.severity}] {finding.package} — {fix}"
    )


def _cmd_check_exceptions(args: argparse.Namespace) -> int:
    path = Path(args.exceptions)
    try:
        exceptions = load_exceptions(path)
    except ExceptionError as error:
        print(f"Exception registry invalid: {error}", file=sys.stderr)
        return 1
    today = _today(args.today)
    expired = [exc for exc in exceptions if not exc.is_active(today)]
    if expired:
        print(f"{len(expired)} expired exception(s) — renew with fresh review or remove:")
        for exc in expired:
            _print_exception(exc)
        return 1
    print(f"Exception registry OK: {len(exceptions)} active entr(y/ies).")
    return 0


def _report(report: PolicyReport) -> None:
    if report.suppressed:
        print(f"{len(report.suppressed)} finding(s) suppressed by active exceptions:")
        for finding, exc in report.suppressed:
            _print_finding(finding)
            print(f"      ↳ accepted by {exc.owner}, expires {exc.expires}: {exc.reason}")
    if report.informational:
        print(f"{len(report.informational)} informational (moderate/low) finding(s):")
        for finding in report.informational:
            _print_finding(finding)
    if report.expired_exceptions:
        print(f"{len(report.expired_exceptions)} EXPIRED exception(s) (a failure):")
        for exc in report.expired_exceptions:
            _print_exception(exc)
    if report.blocking:
        print(f"{len(report.blocking)} BLOCKING finding(s) — release refused:")
        for finding in report.blocking:
            _print_finding(finding)


def _cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        exceptions = load_exceptions(Path(args.exceptions))
    except ExceptionError as error:
        print(f"Exception registry invalid: {error}", file=sys.stderr)
        return 1
    findings: list[Finding] = []
    findings += parse_pip_audit(_load_report(Path(args.pip_audit) if args.pip_audit else None))
    findings += parse_pnpm_audit(_load_report(Path(args.pnpm_audit) if args.pnpm_audit else None))
    report = evaluate(findings, exceptions, today=_today(args.today))
    _report(report)
    if report.passes:
        print(f"Supply-chain gate PASSED: {len(findings)} finding(s), none blocking.")
        return 0
    print("Supply-chain gate FAILED.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supply-chain security gate (SEC-011).")
    parser.add_argument("--today", help="Evaluate as of this ISO date (default: today, UTC).")
    parser.add_argument(
        "--exceptions",
        default=str(DEFAULT_EXCEPTIONS),
        help="Path to the vulnerability exception registry.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-exceptions", help="Validate the exception registry; fail on expiry.")
    evaluate_parser = sub.add_parser("evaluate", help="Apply the policy to scanner reports.")
    evaluate_parser.add_argument("--pip-audit", help="pip-audit --format json output file.")
    evaluate_parser.add_argument("--pnpm-audit", help="pnpm audit --json output file.")

    args = parser.parse_args(argv)
    if args.command == "check-exceptions":
        return _cmd_check_exceptions(args)
    if args.command == "evaluate":
        return _cmd_evaluate(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
