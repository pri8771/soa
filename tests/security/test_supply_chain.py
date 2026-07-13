"""Supply-chain policy engine tests (SEC-011).

The release gate is code, so it is tested like code: the severity policy
(critical/high/unknown block, moderate/low inform), fail-closed handling
of un-scored pip-audit findings, scanner-report parsing for both
ecosystems, and the time-limited exception mechanism — active entries
suppress, expired entries fail, and an exception only ever covers the
advisory it names.
"""

from datetime import date
from pathlib import Path

import pytest

from soa_config.supply_chain import (
    Exception_,
    ExceptionError,
    Finding,
    evaluate,
    load_exceptions,
    parse_pip_audit,
    parse_pnpm_audit,
)

TODAY = date(2026, 7, 13)


def _finding(severity: str, *, ecosystem: str = "pypi", vuln_id: str = "V-1") -> Finding:
    return Finding(
        ecosystem=ecosystem,
        package="lib",
        vulnerability_id=vuln_id,
        severity=severity,
        fix_available=True,
    )


class TestPolicy:
    def test_critical_and_high_block_moderate_and_low_inform(self) -> None:
        findings = [
            _finding("critical", vuln_id="C"),
            _finding("high", vuln_id="H"),
            _finding("moderate", vuln_id="M"),
            _finding("low", vuln_id="L"),
        ]
        report = evaluate(findings, [], today=TODAY)
        assert {f.vulnerability_id for f in report.blocking} == {"C", "H"}
        assert {f.vulnerability_id for f in report.informational} == {"M", "L"}
        assert not report.passes

    def test_unknown_severity_fails_closed(self) -> None:
        # pip-audit findings arrive unscored; the gate must treat them as
        # serious, not wave them through.
        report = evaluate([_finding("unknown")], [], today=TODAY)
        assert len(report.blocking) == 1
        assert not report.passes

    def test_clean_scan_passes(self) -> None:
        report = evaluate([_finding("low"), _finding("moderate")], [], today=TODAY)
        assert report.passes
        assert not report.blocking


class TestExceptions:
    def test_active_exception_suppresses_the_named_finding(self) -> None:
        finding = _finding("critical", vuln_id="CVE-2026-1")
        exc = Exception_(
            id="CVE-2026-1",
            ecosystem="pypi",
            package="lib",
            reason="unreachable code path",
            owner="sec@x",
            added=date(2026, 7, 1),
            expires=date(2026, 12, 31),
        )
        report = evaluate([finding], [exc], today=TODAY)
        assert report.passes
        assert report.suppressed and report.suppressed[0][0] is finding
        assert not report.blocking

    def test_expired_exception_fails_even_without_findings(self) -> None:
        exc = Exception_(
            id="CVE-2026-1",
            ecosystem="pypi",
            package="lib",
            reason="stale",
            owner="sec@x",
            added=date(2026, 1, 1),
            expires=date(2026, 6, 1),  # before TODAY
        )
        report = evaluate([], [exc], today=TODAY)
        assert not report.passes
        assert report.expired_exceptions == (exc,)

    def test_expired_exception_does_not_suppress(self) -> None:
        finding = _finding("critical", vuln_id="CVE-2026-1")
        exc = Exception_(
            id="CVE-2026-1",
            ecosystem="pypi",
            package="lib",
            reason="stale",
            owner="sec@x",
            added=date(2026, 1, 1),
            expires=date(2026, 6, 1),
        )
        report = evaluate([finding], [exc], today=TODAY)
        # The finding blocks (exception is expired) AND the expiry itself
        # is recorded — two independent reasons to refuse.
        assert finding in report.blocking
        assert exc in report.expired_exceptions

    def test_exception_only_covers_matching_id_and_ecosystem(self) -> None:
        finding = _finding("high", ecosystem="npm", vuln_id="1234")
        wrong_ecosystem = Exception_(
            id="1234",
            ecosystem="pypi",
            package="lib",
            reason="x",
            owner="o",
            added=date(2026, 1, 1),
            expires=date(2026, 12, 31),
        )
        report = evaluate([finding], [wrong_ecosystem], today=TODAY)
        assert finding in report.blocking

    def test_exception_matches_via_alias(self) -> None:
        finding = Finding(
            ecosystem="pypi",
            package="lib",
            vulnerability_id="PYSEC-2026-1",
            severity="unknown",
            fix_available=False,
            aliases=frozenset({"CVE-2026-9"}),
        )
        exc = Exception_(
            id="CVE-2026-9",  # names the alias, not the primary id
            ecosystem="pypi",
            package="lib",
            reason="no fix",
            owner="o",
            added=date(2026, 7, 1),
            expires=date(2026, 12, 31),
        )
        report = evaluate([finding], [exc], today=TODAY)
        assert report.passes


class TestRegistryLoading:
    def test_missing_file_is_no_exceptions(self, tmp_path: Path) -> None:
        assert load_exceptions(tmp_path / "nope.toml") == []

    def test_shipped_registry_parses_and_is_empty(self) -> None:
        # The committed registry must always be valid and (by policy)
        # carry no active accepted vulnerabilities.
        registry = (
            Path(__file__).resolve().parents[2] / "security" / "vulnerability-exceptions.toml"
        )
        assert load_exceptions(registry) == []

    def test_valid_entry_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "exc.toml"
        path.write_text(
            "\n".join(
                [
                    "[[exception]]",
                    'id = "CVE-2026-1"',
                    'ecosystem = "npm"',
                    'package = "left-pad"',
                    'reason = "no fix; dev-only"',
                    'owner = "sec@x"',
                    "added = 2026-07-01",
                    "expires = 2026-10-01",
                ]
            ),
            encoding="utf-8",
        )
        (exc,) = load_exceptions(path)
        assert exc.id == "CVE-2026-1"
        assert exc.ecosystem == "npm"
        assert exc.expires == date(2026, 10, 1)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"expires": None},  # missing required field
            {"ecosystem": "linux"},  # ecosystem not pypi/npm
            {"id": ""},  # empty required text
            {"added": "2026-10-01", "expires": "2026-07-01"},  # expires precedes added
        ],
        ids=["missing-field", "bad-ecosystem", "empty-id", "expires-before-added"],
    )
    def test_malformed_entries_raise(self, tmp_path: Path, overrides: dict[str, object]) -> None:
        fields: dict[str, object] = {
            "id": "X",
            "ecosystem": "pypi",
            "package": "p",
            "reason": "r",
            "owner": "o",
            "added": "2026-07-01",
            "expires": "2026-10-01",
        }
        fields.update(overrides)
        lines = ["[[exception]]"]
        for key, value in fields.items():
            if value is None:
                continue  # omit the field entirely
            rendered = value if key in ("added", "expires") else f'"{value}"'
            lines.append(f"{key} = {rendered}")
        path = tmp_path / "bad.toml"
        path.write_text("\n".join(lines), encoding="utf-8")
        with pytest.raises(ExceptionError):
            load_exceptions(path)


class TestScannerParsing:
    def test_pip_audit_parsing(self) -> None:
        report = {
            "dependencies": [
                {"name": "clean", "version": "1.0", "vulns": []},
                {
                    "name": "vulnerable",
                    "version": "2.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2026-1",
                            "fix_versions": ["2.1"],
                            "aliases": ["CVE-2026-9"],
                        },
                        {"id": "PYSEC-2026-2", "fix_versions": []},
                    ],
                },
            ]
        }
        findings = parse_pip_audit(report)
        assert len(findings) == 2
        assert all(f.ecosystem == "pypi" and f.severity == "unknown" for f in findings)
        by_id = {f.vulnerability_id: f for f in findings}
        assert by_id["PYSEC-2026-1"].fix_available is True
        assert "CVE-2026-9" in by_id["PYSEC-2026-1"].aliases
        assert by_id["PYSEC-2026-2"].fix_available is False

    def test_pnpm_legacy_advisories_format(self) -> None:
        # The schema current pnpm actually prints.
        report = {
            "advisories": {
                "1179594": {
                    "id": 1179594,
                    "module_name": "bad-lib",
                    "severity": "critical",
                    "title": "Prototype pollution",
                    "url": "https://npmjs.com/advisories/1179594",
                    "cves": ["CVE-2026-5"],
                    "github_advisory_id": "GHSA-abcd-1234-wxyz",
                    "patched_versions": ">=2.0.0",
                },
                "1179595": {
                    "id": 1179595,
                    "module_name": "no-patch-lib",
                    "severity": "high",
                    "cves": [],
                    "patched_versions": "<0.0.0",  # npm sentinel: no fix
                },
            },
            "metadata": {"vulnerabilities": {"high": 1, "critical": 1}},
        }
        findings = {f.vulnerability_id: f for f in parse_pnpm_audit(report)}
        assert set(findings) == {"GHSA-abcd-1234-wxyz", "1179595"}
        crit = findings["GHSA-abcd-1234-wxyz"]
        assert crit.severity == "critical"
        assert crit.fix_available is True
        assert "CVE-2026-5" in crit.identifiers
        assert findings["1179595"].fix_available is False  # <0.0.0 sentinel

    def test_empty_advisories_falls_through_to_vulnerabilities(self) -> None:
        # pnpm prints an empty `advisories` object on a clean scan; that
        # must not shadow a populated `vulnerabilities` map.
        report = {
            "advisories": {},
            "vulnerabilities": {
                "lib": {
                    "name": "lib",
                    "severity": "high",
                    "via": [{"source": 7, "severity": "high"}],
                }
            },
        }
        findings = parse_pnpm_audit(report)
        assert [f.vulnerability_id for f in findings] == ["7"]

    def test_pnpm_audit_parsing_reads_severity_and_skips_string_via(self) -> None:
        report = {
            "vulnerabilities": {
                "bad-lib": {
                    "name": "bad-lib",
                    "severity": "high",
                    "fixAvailable": True,
                    "via": [
                        {
                            "source": 1234,
                            "name": "bad-lib",
                            "title": "Prototype pollution",
                            "url": "https://example/advisory/1234",
                            "severity": "high",
                        },
                    ],
                },
                "downstream": {
                    "name": "downstream",
                    "severity": "high",
                    "fixAvailable": False,
                    # A pure transitive link — no concrete advisory here.
                    "via": ["bad-lib"],
                },
            }
        }
        findings = parse_pnpm_audit(report)
        assert len(findings) == 1
        (finding,) = findings
        assert finding.ecosystem == "npm"
        assert finding.severity == "high"
        assert finding.vulnerability_id == "1234"
        assert finding.fix_available is True

    def test_empty_or_missing_reports_yield_nothing(self) -> None:
        assert parse_pip_audit({}) == []
        assert parse_pnpm_audit({}) == []

    def test_end_to_end_npm_moderate_is_informational(self) -> None:
        report = {
            "vulnerabilities": {
                "lib": {
                    "name": "lib",
                    "severity": "moderate",
                    "fixAvailable": True,
                    "via": [{"source": 9, "severity": "moderate", "title": "ReDoS"}],
                }
            }
        }
        decision = evaluate(parse_pnpm_audit(report), [], today=TODAY)
        assert decision.passes
        assert len(decision.informational) == 1
