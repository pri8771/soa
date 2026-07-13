"""Deterministic model generation from the canonical schema (CAN-002).

The JSON Schema is the source of truth; the Python TypedDicts and the
TypeScript interfaces are GENERATED from it, committed, and drift-checked
in tests — editing them by hand cannot survive CI. Generation is a pure
function of the schema file: no timestamps, no environment, property
order taken from the schema document itself, so the output is
byte-for-byte reproducible.

Regenerate after changing the schema:

    uv run python -m soa_canonical.generate

The generator intentionally supports only the constructs the canonical
schema uses (closed objects, $defs references, ``anyOf [X, null]``
nullability, const/enum, arrays, string/integer scalars, and the two
open maps). An unsupported construct raises instead of guessing.
"""

from pathlib import Path
from typing import Any

from soa_canonical import CURRENT_VERSION, load_schema

PY_HEADER = (
    '"""GENERATED from the canonical order schema — DO NOT EDIT.\n'
    "\n"
    "Source: packages/canonical/src/soa_canonical/schemas/ (version {version})\n"
    "Regenerate: uv run python -m soa_canonical.generate\n"
    'The drift test (packages/canonical/tests/test_generation.py) fails CI on edits."""\n'
)

TS_HEADER = (
    "// GENERATED from the canonical order schema — DO NOT EDIT.\n"
    "// Source: packages/canonical/src/soa_canonical/schemas/ (version {version})\n"
    "// Regenerate: uv run python -m soa_canonical.generate\n"
    "// The drift test (packages/canonical/tests/test_generation.py) fails CI on edits.\n"
)


class UnsupportedSchemaConstructError(Exception):
    def __init__(self, path: str, spec: Any) -> None:
        super().__init__(f"generator cannot express {path}: {spec!r}")


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


#: Scalar $defs collapse to plain strings in both languages; their
#: constraints live in the schema, enforced by validate_order.
_SCALAR_DEFS = {"decimal", "currency_code", "iso_date", "uuid"}


class _Generator:
    """Shared walk; language-specific emission. Nested inline objects
    become named types registered in first-encounter order."""

    def __init__(self, schema: dict[str, Any], *, lang: str) -> None:
        self.schema = schema
        self.lang = lang
        self.blocks: list[str] = []

    # -- type resolution -------------------------------------------------

    def resolve(self, spec: dict[str, Any], path: str, hint: str) -> str:
        if "$ref" in spec:
            name = spec["$ref"].rsplit("/", 1)[-1]
            if name in _SCALAR_DEFS:
                return "str" if self.lang == "py" else "string"
            if name.startswith("nullable_"):
                inner = self.resolve(
                    {"$ref": f"#/$defs/{name.removeprefix('nullable_')}"}, path, hint
                )
                return f"{inner} | None" if self.lang == "py" else f"{inner} | null"
            return _pascal(name)
        if "const" in spec:
            value = spec["const"]
            return f'Literal["{value}"]' if self.lang == "py" else f'"{value}"'
        if "enum" in spec:
            values = spec["enum"]
            if self.lang == "py":
                return "Literal[" + ", ".join(f'"{v}"' for v in values) + "]"
            return " | ".join(f'"{v}"' for v in values)
        if "anyOf" in spec:
            return " | ".join(
                self.resolve(branch, f"{path}.anyOf[{i}]", hint)
                for i, branch in enumerate(spec["anyOf"])
            )
        declared = spec.get("type")
        if declared == "null":
            return "None" if self.lang == "py" else "null"
        if isinstance(declared, list):
            parts = [self.resolve({**spec, "type": t}, path, hint) for t in declared]
            return " | ".join(parts)
        if declared == "string":
            return "str" if self.lang == "py" else "string"
        if declared == "integer":
            return "int" if self.lang == "py" else "number"
        if declared == "boolean":
            return "bool" if self.lang == "py" else "boolean"
        if declared == "array":
            item = self.resolve(spec.get("items", {}), f"{path}[]", hint)
            if self.lang == "py":
                return f"list[{item}]"
            return f"({item})[]" if ("|" in item or " " in item) else f"{item}[]"
        if declared == "object":
            if "properties" in spec:
                self.emit_object(hint, spec, path)
                return hint
            # Open maps: provenance (typed values) and extensions (free).
            extra = spec.get("additionalProperties")
            if isinstance(extra, dict):
                value = self.resolve(extra, f"{path}.additionalProperties", hint)
                return f"dict[str, {value}]" if self.lang == "py" else f"Record<string, {value}>"
            return "dict[str, Any]" if self.lang == "py" else "Record<string, unknown>"
        if not declared and spec == {}:
            return "Any" if self.lang == "py" else "unknown"
        raise UnsupportedSchemaConstructError(path, spec)

    # -- object emission -------------------------------------------------

    def emit_object(self, name: str, spec: dict[str, Any], path: str) -> None:
        required = set(spec.get("required", []))
        lines: list[str] = []
        for prop, prop_spec in spec.get("properties", {}).items():
            resolved = self.resolve(prop_spec, f"{path}.{prop}", name + _pascal(prop))
            if self.lang == "py":
                declared = resolved if prop in required else f"NotRequired[{resolved}]"
                lines.append(f"    {prop}: {declared}")
            else:
                marker = "" if prop in required else "?"
                lines.append(f"  {prop}{marker}: {resolved};")
        if self.lang == "py":
            body = "\n".join(lines) if lines else "    pass"
            self.blocks.append(f"class {name}(TypedDict):\n{body}\n")
        else:
            body = "\n".join(lines)
            self.blocks.append(f"export interface {name} {{\n{body}\n}}\n")

    # -- whole-document walk ----------------------------------------------

    def generate(self) -> str:
        # Named $defs first (objects only; scalars collapse to strings).
        for def_name, def_spec in self.schema.get("$defs", {}).items():
            if def_name in _SCALAR_DEFS or def_name.startswith("nullable_"):
                continue
            self.emit_object(_pascal(def_name), def_spec, f"$defs.{def_name}")
        self.emit_object("CanonicalOrder", self.schema, "$")
        if self.lang == "py":
            header = PY_HEADER.format(version=CURRENT_VERSION)
            # Annotations stay strings at runtime (nested types may be
            # declared in schema order, after their first reference).
            imports = (
                "\nfrom __future__ import annotations\n"
                "\nfrom typing import Any, Literal, NotRequired, TypedDict\n\n\n"
            )
            return header + imports + "\n\n".join(self.blocks)
        header = TS_HEADER.format(version=CURRENT_VERSION)
        return header + "\n" + "\n".join(self.blocks)


def python_source(schema: dict[str, Any] | None = None) -> str:
    return _Generator(schema or load_schema(), lang="py").generate()


def typescript_source(schema: dict[str, Any] | None = None) -> str:
    return _Generator(schema or load_schema(), lang="ts").generate()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


PY_TARGET = "packages/canonical/src/soa_canonical/models.py"
TS_TARGET = "apps/web/src/api/canonical-order.ts"


def write_models(root: Path | None = None) -> list[Path]:
    base = root or _repo_root()
    py_path = base / PY_TARGET
    ts_path = base / TS_TARGET
    py_path.write_text(python_source(), encoding="utf-8")
    ts_path.write_text(typescript_source(), encoding="utf-8")
    return [py_path, ts_path]


if __name__ == "__main__":
    for path in write_models():
        print(f"wrote {path}")
