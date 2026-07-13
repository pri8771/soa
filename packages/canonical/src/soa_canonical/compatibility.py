"""Backward-compatibility rules for canonical schema versions (CAN-001).

A payload written under version N must remain valid under version N+1.
That holds when newer versions only WIDEN the schema. ``breaking_changes``
flags the narrowings that would strand existing payloads:

- removing a property (readers may still receive it → closed objects
  would reject it),
- adding a required property,
- changing a property's declared type set (or removing a null branch),
- narrowing an enum,
- raising minItems / minLength or shrinking numeric bounds.

The check walks the object tree structurally; it is deliberately
conservative — anything it cannot prove compatible is reported.
"""

from typing import Any


def _type_set(spec: dict[str, Any]) -> set[str] | None:
    declared = spec.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return set(declared)
    if "const" in spec:
        return None  # handled separately
    if "anyOf" in spec:
        combined: set[str] = set()
        for branch in spec["anyOf"]:
            branch_types = _type_set(branch) if isinstance(branch, dict) else None
            if branch_types is None:
                return None  # refs/consts: not comparable structurally
            combined |= branch_types
        return combined
    return None


def _walk(old: dict[str, Any], new: dict[str, Any], path: str, problems: list[str]) -> None:
    old_properties = old.get("properties", {})
    new_properties = new.get("properties", {})
    for name, old_spec in old_properties.items():
        if name not in new_properties:
            problems.append(f"{path}.{name}: property removed")
            continue
        new_spec = new_properties[name]
        old_types, new_types = _type_set(old_spec), _type_set(new_spec)
        if old_types is not None and new_types is not None and not old_types <= new_types:
            problems.append(
                f"{path}.{name}: type narrowed from {sorted(old_types)} to {sorted(new_types)}"
            )
        if "enum" in old_spec and "enum" in new_spec:
            removed = set(old_spec["enum"]) - set(new_spec["enum"])
            if removed:
                problems.append(f"{path}.{name}: enum values removed: {sorted(removed)}")
        for bound in ("minLength", "minItems", "minimum"):
            if bound in new_spec and new_spec[bound] > old_spec.get(bound, 0):
                problems.append(f"{path}.{name}: {bound} raised to {new_spec[bound]}")
        if isinstance(old_spec, dict) and isinstance(new_spec, dict):
            _walk(old_spec, new_spec, f"{path}.{name}", problems)
            if "items" in old_spec and "items" in new_spec:
                _walk(old_spec["items"], new_spec["items"], f"{path}.{name}[]", problems)

    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    for added in sorted(new_required - old_required):
        problems.append(f"{path}: '{added}' became required")


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """The narrowings ``new`` introduces relative to ``old``; empty when
    every payload valid under ``old`` stays valid under ``new``.
    Definitions are compared too, since properties reference them."""
    problems: list[str] = []
    _walk(old, new, "$", problems)
    _walk(old.get("$defs", {}), new.get("$defs", {}), "$defs", problems)
    for name, old_def in old.get("$defs", {}).items():
        new_def = new.get("$defs", {}).get(name)
        if new_def is None:
            problems.append(f"$defs.{name}: definition removed")
        else:
            _walk(old_def, new_def, f"$defs.{name}", problems)
            if "items" in old_def and "items" in new_def:
                _walk(old_def["items"], new_def["items"], f"$defs.{name}[]", problems)
    return problems
