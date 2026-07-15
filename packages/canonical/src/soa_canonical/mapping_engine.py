"""Deterministic mapping engine (EXP-002).

Executes a DECLARATIVE mapping profile over a canonical order to build
the target payload an integration delivers. Three hard properties:

- NO ARBITRARY CODE. The definition is data: field mappings (dot paths
  into the canonical payload), constants, a closed registry of
  formatting transforms, declarative conditions, and defaults. An
  unknown transform or key is a definition error, never a fallback.
- EVERY RUN IS TRACED. The result carries one trace entry per produced
  value: the source path, the raw value, what transforms ran, whether a
  condition or default decided the outcome. Deliveries can show their
  work.
- EXECUTION IS BOUNDED. Field/constant/line counts and output size have
  hard caps; the engine walks the definition once — no recursion into
  data, no loops the definition can inflate.

The mapped payload is validated against the profile's TARGET schema
before it is returned; violations are named errors, not deliveries.
"""

import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

# -- bounds (defense against runaway definitions) ---------------------------------
MAX_FIELDS = 500
MAX_CONSTANTS = 100
MAX_LINE_ITEMS = 5000
MAX_OUTPUT_BYTES = 1_000_000
#: Line-level trace entries are capped; the cap is stated in the trace.
MAX_TRACED_ROWS = 50

FORMAT_KINDS = frozenset(
    {"decimal", "date", "uppercase", "lowercase", "trim", "prefix", "suffix", "map"}
)
_FIELD_KEYS = frozenset({"target", "source", "format", "default", "when", "required"})
_CONDITION_KEYS = frozenset({"present", "equals"})
_DATE_TOKENS = ("YYYY", "MM", "DD")


class MappingDefinitionError(Exception):
    """The mapping definition itself is malformed. ``errors`` names every
    problem; nothing executes until the definition is clean."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        preview = "; ".join(errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        super().__init__(f"mapping definition invalid: {preview}{more}")


class MappingExecutionError(Exception):
    """The mapping could not produce a valid target payload for THIS
    canonical order (missing required values, target-schema violations)."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        preview = "; ".join(errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        super().__init__(f"mapping execution failed: {preview}{more}")


@dataclass(frozen=True)
class TraceEntry:
    target: str
    source: str | None
    raw: Any
    value: Any
    applied: tuple[str, ...] = ()
    used_default: bool = False
    condition_passed: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "source": self.source,
            "raw": self.raw,
            "value": self.value,
            "applied": list(self.applied),
            "used_default": self.used_default,
            "condition_passed": self.condition_passed,
        }


@dataclass
class MappingResult:
    payload: dict[str, Any]
    trace: list[TraceEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# -- definition validation ----------------------------------------------------------


def _check_field(spec: Any, where: str, errors: list[str]) -> None:
    if not isinstance(spec, dict):
        errors.append(f"{where}: a field mapping must be an object")
        return
    unknown = set(spec) - _FIELD_KEYS
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)}")
    if not isinstance(spec.get("target"), str) or not spec.get("target"):
        errors.append(f"{where}: 'target' must be a non-empty string")
    if not isinstance(spec.get("source"), str) or not spec.get("source"):
        errors.append(f"{where}: 'source' must be a non-empty dot path")
    fmt = spec.get("format")
    if fmt is not None:
        if not isinstance(fmt, dict) or fmt.get("kind") not in FORMAT_KINDS:
            errors.append(
                f"{where}: unknown format {fmt!r} — kinds: {', '.join(sorted(FORMAT_KINDS))}"
            )
        elif fmt["kind"] == "decimal" and not isinstance(fmt.get("scale", 2), int):
            errors.append(f"{where}: decimal 'scale' must be an integer")
        elif fmt["kind"] == "date":
            pattern = fmt.get("pattern", "YYYY-MM-DD")
            stripped = pattern
            for token in (*_DATE_TOKENS, "-", "/", "."):
                stripped = stripped.replace(token, "")
            if stripped:
                errors.append(
                    f"{where}: date pattern may only use "
                    f"{', '.join(_DATE_TOKENS)} and -/. separators"
                )
        elif fmt["kind"] in ("prefix", "suffix") and not isinstance(fmt.get("value"), str):
            errors.append(f"{where}: {fmt['kind']} needs a string 'value'")
        elif fmt["kind"] == "map" and not isinstance(fmt.get("values"), dict):
            errors.append(f"{where}: map needs a 'values' object")
    when = spec.get("when")
    if when is not None:
        if not isinstance(when, dict) or not set(when) or set(when) - _CONDITION_KEYS:
            errors.append(f"{where}: 'when' supports {sorted(_CONDITION_KEYS)} — got {when!r}")
        elif "equals" in when and (
            not isinstance(when["equals"], dict) or not isinstance(when["equals"].get("path"), str)
        ):
            errors.append(f"{where}: 'when.equals' needs {{path, value}}")


def validate_mapping_definition(definition: Any) -> list[str]:
    """Every structural problem in the definition, by location. An empty
    list means the definition can execute."""
    errors: list[str] = []
    if not isinstance(definition, dict):
        return ["the mapping definition must be an object"]
    unknown = set(definition) - {"fields", "constants", "lines"}
    if unknown:
        errors.append(f"$: unknown keys {sorted(unknown)}")
    fields_spec = definition.get("fields", [])
    if not isinstance(fields_spec, list):
        errors.append("$.fields: must be a list")
        fields_spec = []
    if len(fields_spec) > MAX_FIELDS:
        errors.append(f"$.fields: at most {MAX_FIELDS} field mappings")
    for index, spec in enumerate(fields_spec):
        _check_field(spec, f"$.fields[{index}]", errors)
    constants = definition.get("constants", [])
    if not isinstance(constants, list):
        errors.append("$.constants: must be a list")
        constants = []
    if len(constants) > MAX_CONSTANTS:
        errors.append(f"$.constants: at most {MAX_CONSTANTS} constants")
    for index, spec in enumerate(constants):
        if (
            not isinstance(spec, dict)
            or set(spec) - {"target", "value"}
            or not isinstance(spec.get("target"), str)
            or "value" not in spec
        ):
            errors.append(f"$.constants[{index}]: needs exactly {{target, value}}")
    lines = definition.get("lines")
    if lines is not None:
        if not isinstance(lines, dict) or set(lines) - {"source", "target", "fields"}:
            errors.append("$.lines: needs {source, target, fields}")
        else:
            if lines.get("source") != "line_items":
                errors.append("$.lines.source: only 'line_items' exists on a canonical order")
            if not isinstance(lines.get("target"), str) or not lines.get("target"):
                errors.append("$.lines.target: must be a non-empty string")
            line_fields = lines.get("fields", [])
            if not isinstance(line_fields, list) or not line_fields:
                errors.append("$.lines.fields: must be a non-empty list")
            else:
                for index, spec in enumerate(line_fields):
                    _check_field(spec, f"$.lines.fields[{index}]", errors)
    return errors


# -- execution ------------------------------------------------------------------------


def _lookup(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isascii() and part.isdigit() and len(part) <= 9:
            index = int(part)
            if index < len(current):
                current = current[index]
                continue
            return None
        return None
    return current


def _format_date(value: str, pattern: str, where: str, errors: list[str]) -> str | None:
    parts = value.split("-")
    if len(parts) != 3:
        errors.append(f"{where}: {value!r} is not an ISO date; cannot reformat")
        return None
    year, month, day = parts
    return pattern.replace("YYYY", year).replace("MM", month).replace("DD", day)


def _apply_format(
    value: Any, fmt: dict[str, Any], where: str, errors: list[str]
) -> tuple[Any, bool]:
    """(formatted value, ok). Transforms never guess: a value the
    transform cannot handle is an execution error naming the field."""
    kind = fmt["kind"]
    if kind == "decimal":
        try:
            quantum = Decimal(1).scaleb(-int(fmt.get("scale", 2)))
            return str(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)), True
        except (InvalidOperation, ValueError):
            errors.append(f"{where}: {value!r} is not a decimal")
            return None, False
    if kind == "date":
        formatted = _format_date(str(value), fmt.get("pattern", "YYYY-MM-DD"), where, errors)
        return formatted, formatted is not None
    if kind == "uppercase":
        return str(value).upper(), True
    if kind == "lowercase":
        return str(value).lower(), True
    if kind == "trim":
        return str(value).strip(), True
    if kind == "prefix":
        return f"{fmt['value']}{value}", True
    if kind == "suffix":
        return f"{value}{fmt['value']}", True
    if kind == "map":
        values = fmt["values"]
        key = str(value)
        if key in values:
            return values[key], True
        if "default" in fmt:
            return fmt["default"], True
        errors.append(
            f"{where}: {value!r} has no mapping (known: {', '.join(sorted(values))}) and no default"
        )
        return None, False
    raise AssertionError(f"unvalidated format kind {kind!r}")  # pragma: no cover


def _condition_passes(when: dict[str, Any] | None, scope: Any) -> bool | None:
    if when is None:
        return None
    if "present" in when:
        return _lookup(scope, when["present"]) is not None
    target = when["equals"]
    return bool(_lookup(scope, target["path"]) == target.get("value"))


def _run_field(
    spec: dict[str, Any],
    scope: Any,
    out: dict[str, Any],
    trace: list[TraceEntry],
    errors: list[str],
    where: str,
    *,
    traced: bool = True,
) -> None:
    target = spec["target"]
    condition = _condition_passes(spec.get("when"), scope)
    if condition is False:
        if traced:
            trace.append(
                TraceEntry(
                    target=target,
                    source=spec["source"],
                    raw=None,
                    value=None,
                    condition_passed=False,
                )
            )
        return
    raw = _lookup(scope, spec["source"])
    value = raw
    used_default = False
    applied: tuple[str, ...] = ()
    if value is None and "default" in spec:
        value = spec["default"]
        used_default = True
    elif value is not None and spec.get("format") is not None:
        value, ok = _apply_format(value, spec["format"], where, errors)
        if not ok:
            return
        applied = (spec["format"]["kind"],)
    if value is None:
        if spec.get("required"):
            errors.append(f"{where}: required source {spec['source']!r} has no value")
        return
    out[target] = value
    if traced:
        trace.append(
            TraceEntry(
                target=target,
                source=spec["source"],
                raw=raw,
                value=value,
                applied=applied,
                used_default=used_default,
                condition_passed=condition,
            )
        )


def execute_mapping(
    definition: dict[str, Any],
    canonical: dict[str, Any],
    *,
    target_schema: dict[str, Any] | None = None,
) -> MappingResult:
    """Run the mapping over a canonical order. Raises
    MappingDefinitionError for a malformed definition and
    MappingExecutionError when THIS order cannot produce a valid target
    payload; otherwise returns the payload with its full trace."""
    definition_errors = validate_mapping_definition(definition)
    if definition_errors:
        raise MappingDefinitionError(definition_errors)

    errors: list[str] = []
    result = MappingResult(payload={})
    for index, spec in enumerate(definition.get("fields", [])):
        _run_field(spec, canonical, result.payload, result.trace, errors, f"$.fields[{index}]")
    for spec in definition.get("constants", []):
        result.payload[spec["target"]] = spec["value"]
        result.trace.append(
            TraceEntry(target=spec["target"], source=None, raw=None, value=spec["value"])
        )

    lines = definition.get("lines")
    if lines is not None:
        items = canonical.get("line_items", [])
        if len(items) > MAX_LINE_ITEMS:
            raise MappingExecutionError(
                [f"line_items: {len(items)} rows exceeds the {MAX_LINE_ITEMS} cap"]
            )
        mapped_rows: list[dict[str, Any]] = []
        for row_index, item in enumerate(items):
            row: dict[str, Any] = {}
            traced = row_index < MAX_TRACED_ROWS
            for field_index, spec in enumerate(lines["fields"]):
                _run_field(
                    spec,
                    item,
                    row,
                    result.trace,
                    errors,
                    f"$.lines.fields[{field_index}] (row {row_index + 1})",
                    traced=traced,
                )
            mapped_rows.append(row)
        result.payload[lines["target"]] = mapped_rows
        if len(items) > MAX_TRACED_ROWS:
            result.notes.append(
                f"trace covers the first {MAX_TRACED_ROWS} of {len(items)} line rows"
            )

    if errors:
        raise MappingExecutionError(sorted(errors))

    serialized = json.dumps(result.payload)
    if len(serialized.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise MappingExecutionError([f"mapped payload exceeds the {MAX_OUTPUT_BYTES} byte bound"])

    if target_schema:
        validator = Draft202012Validator(target_schema, format_checker=FormatChecker())
        violations = sorted(
            validator.iter_errors(result.payload), key=lambda error: list(error.absolute_path)
        )
        if violations:
            raise MappingExecutionError(
                [
                    "target-schema: "
                    + "/".join(str(part) for part in violation.absolute_path)
                    + f": {violation.message}"
                    for violation in violations
                ]
            )
    return result
