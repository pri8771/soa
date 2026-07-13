"""Supply-chain vulnerability policy engine (SEC-011).

Turns raw scanner output into a RELEASE DECISION under one written
policy, so the gate is code — reviewable and unit-tested — rather than a
scanner's default exit code.

Inputs are the JSON reports two scanners already produce:

- ``pip-audit`` for the Python dependency closure. The tool does not
  score advisories, so every Python finding arrives as ``unknown``
  severity;
- ``pnpm audit`` for the JavaScript closure, whose advisories DO carry a
  CVSS-derived severity.

The policy (``docs/SUPPLY_CHAIN_SECURITY.md`` is the prose):

- ``critical`` and ``high`` findings BLOCK the release;
- ``unknown`` severity BLOCKS too — fail closed: an advisory we cannot
  score is treated as if it were serious until someone proves otherwise.
  This is why every un-excepted pip-audit finding blocks;
- ``moderate`` / ``low`` are INFORMATIONAL — reported, never blocking.

A finding stops blocking only when a TIME-LIMITED exception names it: an
entry in ``security/vulnerability-exceptions.toml`` carrying an owner, a
justification, and an ``expires`` date. An EXPIRED exception is itself a
failure — accepted risk is renewed with fresh review, never left to
rot silently. Exceptions match by advisory id OR any of a finding's
aliases (CVE ↔ GHSA), scoped to the ecosystem, so one entry covers the
same advisory however a scanner names it.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

__all__ = [
    "BLOCKING_SEVERITIES",
    "INFORMATIONAL_SEVERITIES",
    "ExceptionError",
    "Exception_",
    "Finding",
    "PolicyReport",
    "evaluate",
    "load_exceptions",
    "parse_pip_audit",
    "parse_pnpm_audit",
]

#: Severities that block a release when not covered by an active
#: exception. ``unknown`` is here on purpose (fail closed).
BLOCKING_SEVERITIES = frozenset({"critical", "high", "unknown"})
#: Severities we report but never block on.
INFORMATIONAL_SEVERITIES = frozenset({"moderate", "medium", "low"})

_SEVERITY_ALIASES = {"medium": "moderate"}


def _normalize_severity(raw: str | None) -> str:
    if not raw:
        return "unknown"
    value = raw.strip().lower()
    return _SEVERITY_ALIASES.get(value, value)


@dataclass(frozen=True)
class Finding:
    """A single advisory against one dependency, ecosystem-normalized."""

    ecosystem: str  # "pypi" | "npm"
    package: str
    vulnerability_id: str
    severity: str
    fix_available: bool
    aliases: frozenset[str] = frozenset()

    @property
    def is_blocking_severity(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    @property
    def identifiers(self) -> frozenset[str]:
        return frozenset({self.vulnerability_id}) | self.aliases


class ExceptionError(ValueError):
    """A malformed or expired exception registry entry."""


@dataclass(frozen=True)
class Exception_:
    """A time-limited, attributed acceptance of a known finding.

    Named ``Exception_`` to avoid shadowing the builtin; the registry
    key is the plain word ``exception``.
    """

    id: str
    ecosystem: str
    package: str
    reason: str
    owner: str
    added: date
    expires: date

    def is_active(self, today: date) -> bool:
        """Active through the whole of its expiry day (inclusive)."""
        return today <= self.expires

    def covers(self, finding: Finding) -> bool:
        return self.ecosystem == finding.ecosystem and self.id in finding.identifiers


@dataclass(frozen=True)
class PolicyReport:
    """The release decision. ``blocking`` empty ⇒ the gate passes."""

    blocking: tuple[Finding, ...] = ()
    suppressed: tuple[tuple[Finding, Exception_], ...] = ()
    informational: tuple[Finding, ...] = ()
    expired_exceptions: tuple[Exception_, ...] = ()

    @property
    def passes(self) -> bool:
        return not self.blocking and not self.expired_exceptions


def _require(entry: Mapping[str, Any], key: str, index: int) -> Any:
    if key not in entry:
        raise ExceptionError(f"exception #{index}: missing required field {key!r}")
    return entry[key]


def _parse_date(value: Any, key: str, index: int) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ExceptionError(
                f"exception #{index}: {key} is not an ISO date: {value!r}"
            ) from error
    raise ExceptionError(f"exception #{index}: {key} must be a date, got {type(value).__name__}")


def load_exceptions(path: Path) -> list[Exception_]:
    """Parse and validate the exception registry.

    A missing file means "no exceptions" (the common case). A present
    file with a malformed entry raises :class:`ExceptionError` — a
    broken registry must never silently suppress nothing OR everything.
    """
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_entries = data.get("exception", [])
    if not isinstance(raw_entries, list):
        raise ExceptionError("`exception` must be an array of tables")
    exceptions: list[Exception_] = []
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, Mapping):
            raise ExceptionError(f"exception #{index}: must be a table")
        ecosystem = str(_require(entry, "ecosystem", index))
        if ecosystem not in ("pypi", "npm"):
            raise ExceptionError(f"exception #{index}: ecosystem must be 'pypi' or 'npm'")
        for text_key in ("id", "package", "reason", "owner"):
            if not str(_require(entry, text_key, index)).strip():
                raise ExceptionError(f"exception #{index}: {text_key} must be non-empty")
        added = _parse_date(_require(entry, "added", index), "added", index)
        expires = _parse_date(_require(entry, "expires", index), "expires", index)
        if expires < added:
            raise ExceptionError(f"exception #{index}: expires {expires} precedes added {added}")
        exceptions.append(
            Exception_(
                id=str(entry["id"]).strip(),
                ecosystem=ecosystem,
                package=str(entry["package"]).strip(),
                reason=str(entry["reason"]).strip(),
                owner=str(entry["owner"]).strip(),
                added=added,
                expires=expires,
            )
        )
    return exceptions


def parse_pip_audit(report: Mapping[str, Any]) -> list[Finding]:
    """Findings from ``pip-audit --format json``.

    Shape: ``{"dependencies": [{"name", "version", "vulns": [{"id",
    "fix_versions", "aliases"}]}]}``. pip-audit does not score
    advisories, so severity is always ``unknown`` (fail closed).
    """
    findings: list[Finding] = []
    dependencies = report.get("dependencies", [])
    if not isinstance(dependencies, list):
        return findings
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            continue
        name = str(dependency.get("name", "")).strip() or "(unknown)"
        for vuln in dependency.get("vulns", []) or []:
            if not isinstance(vuln, Mapping):
                continue
            vuln_id = str(vuln.get("id", "")).strip()
            if not vuln_id:
                continue
            fix_versions = vuln.get("fix_versions") or []
            aliases = frozenset(
                str(a).strip() for a in (vuln.get("aliases") or []) if str(a).strip()
            )
            findings.append(
                Finding(
                    ecosystem="pypi",
                    package=name,
                    vulnerability_id=vuln_id,
                    severity="unknown",
                    fix_available=bool(fix_versions),
                    aliases=aliases,
                )
            )
    return findings


def _fix_available(value: Any) -> bool:
    # npm reports fixAvailable as either a bool or an object describing
    # the fix; either truthy value means a fix exists.
    return bool(value)


def parse_pnpm_audit(report: Mapping[str, Any]) -> list[Finding]:
    """Findings from ``pnpm audit --json``.

    pnpm can emit either schema, so both are handled:

    - the **legacy ``advisories``** map (what current pnpm actually
      prints): ``{"advisories": {"<id>": {"module_name", "severity",
      "cves", "github_advisory_id", "patched_versions", "url"}}}``;
    - the **npm v2 ``vulnerabilities``** map:
      ``{"vulnerabilities": {"<pkg>": {"name", "severity", "via":
      [advisory-object | "<pkg-name>"], "fixAvailable"}}}`` — only object
      ``via`` entries are concrete advisories.
    """
    advisories = report.get("advisories")
    if isinstance(advisories, Mapping) and advisories:
        return _parse_pnpm_advisories(advisories)
    vulnerabilities = report.get("vulnerabilities")
    if isinstance(vulnerabilities, Mapping):
        return _parse_pnpm_vulnerabilities(vulnerabilities)
    return []


def _parse_pnpm_advisories(advisories: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for key, advisory in advisories.items():
        if not isinstance(advisory, Mapping):
            continue
        vuln_id = str(advisory.get("github_advisory_id") or advisory.get("id") or key).strip()
        if not vuln_id:
            continue
        package = str(advisory.get("module_name", "")).strip() or "(unknown)"
        severity = _normalize_severity(str(advisory.get("severity") or ""))
        # npm marks "no patch available" with the sentinel "<0.0.0".
        patched = str(advisory.get("patched_versions") or "").strip()
        fix_available = bool(patched) and patched != "<0.0.0"
        aliases: set[str] = {str(advisory.get("id")).strip()} if advisory.get("id") else set()
        aliases |= {str(cve).strip() for cve in (advisory.get("cves") or []) if str(cve).strip()}
        if advisory.get("url"):
            aliases.add(str(advisory["url"]).strip())
        aliases.discard(vuln_id)
        findings.append(
            Finding(
                ecosystem="npm",
                package=package,
                vulnerability_id=vuln_id,
                severity=severity,
                fix_available=fix_available,
                aliases=frozenset(aliases),
            )
        )
    return findings


def _parse_pnpm_vulnerabilities(vulnerabilities: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for pkg_name, node in vulnerabilities.items():
        if not isinstance(node, Mapping):
            continue
        package = str(node.get("name", pkg_name)).strip() or str(pkg_name)
        fix_available = _fix_available(node.get("fixAvailable"))
        for via in node.get("via", []) or []:
            if not isinstance(via, Mapping):
                continue  # string via = link to another package's advisory
            source = via.get("source")
            vuln_id = (
                str(source).strip()
                if source is not None
                else str(via.get("url") or via.get("title") or "").strip()
            )
            if not vuln_id:
                continue
            severity = _normalize_severity(str(via.get("severity") or node.get("severity") or ""))
            key = (package, vuln_id)
            if key in seen:
                continue
            seen.add(key)
            aliases = frozenset(
                str(via.get(k)).strip() for k in ("cwe", "url", "title") if via.get(k)
            )
            findings.append(
                Finding(
                    ecosystem="npm",
                    package=package,
                    vulnerability_id=vuln_id,
                    severity=severity,
                    fix_available=fix_available,
                    aliases=aliases,
                )
            )
    return findings


def evaluate(
    findings: Iterable[Finding],
    exceptions: Iterable[Exception_],
    *,
    today: date,
) -> PolicyReport:
    """Apply the policy to findings, given the exception registry."""
    exception_list = list(exceptions)
    active = [exc for exc in exception_list if exc.is_active(today)]
    expired = tuple(exc for exc in exception_list if not exc.is_active(today))

    blocking: list[Finding] = []
    suppressed: list[tuple[Finding, Exception_]] = []
    informational: list[Finding] = []
    for finding in findings:
        if not finding.is_blocking_severity:
            informational.append(finding)
            continue
        covering = next((exc for exc in active if exc.covers(finding)), None)
        if covering is not None:
            suppressed.append((finding, covering))
        else:
            blocking.append(finding)
    return PolicyReport(
        blocking=tuple(blocking),
        suppressed=tuple(suppressed),
        informational=tuple(informational),
        expired_exceptions=expired,
    )
