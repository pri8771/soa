"""Deterministic, bounded, traced rule evaluation (PRC-009).

Executes rule conditions over canonical values (PRC-008): exact Decimal
arithmetic (never floats), ISO dates that compare lexically, money as
{"amount", "currency"} dicts. Runs the CFG-004 authoring ops (field,
const, comparisons, and/or/not, is_present) plus runtime ops the
baseline sales-order rules need: add/sub/mul/div, sum/row_count over
table rows, and approx_eq with EXPLICIT absolute/relative tolerances.

Three-valued honesty: a condition over missing or incomparable values is
INDETERMINATE, not silently false. Findings say "triggered", "passed",
or "indeterminate", and the routing policy (PRC-011) decides what
indeterminate means for a critical rule. Every finding carries a trace —
the expression tree annotated with each operand's resolved value — so
the result is explainable without re-running anything.

Complexity is bounded up front (nodes per rule, depth, rule count, rows)
and during evaluation (a step budget and a fixed-precision Decimal
context), so a malicious or runaway rule set fails fast with
RuleComplexityError instead of consuming the worker.
"""

import decimal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

SEVERITIES = frozenset({"error", "warning", "info"})
ACTIONS = frozenset({"block", "route_to_review", "annotate"})
COMPARISONS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
ARITHMETIC = frozenset({"add", "sub", "mul", "div"})

#: Fixed-precision context: enough for any sane order, small enough that
#: multiplication chains cannot balloon. Overflow -> indeterminate.
_DECIMAL_CONTEXT = decimal.Context(prec=34, Emin=-6143, Emax=6144)
_MAX_LITERAL_LENGTH = 40


class RuleDefinitionError(ValueError):
    """The rule set itself is malformed (not a data problem)."""


class RuleComplexityError(ValueError):
    """The rule set exceeds the complexity limits."""


@dataclass(frozen=True)
class EvaluationLimits:
    max_rules: int = 500
    max_nodes_per_rule: int = 200
    max_depth: int = 25
    max_rows_per_table: int = 5000
    #: Total node evaluations across the whole rule set (rules x rows).
    step_budget: int = 1_000_000


@dataclass(frozen=True)
class EvaluationInput:
    """Canonical values keyed by flat schema key. Table rows are dicts
    keyed by the FULL column key ("lines.quantity"), matching how
    PRC-007 stores cells."""

    header: Mapping[str, Any]
    tables: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class DerivedValue:
    field_key: str
    row_index: int | None
    value: Any


@dataclass(frozen=True)
class RuleFinding:
    rule_key: str
    severity: str
    action: str
    #: "triggered" (condition true), "passed" (false), or "indeterminate"
    #: (missing/incomparable operands — the trace says which).
    status: str
    row_index: int | None
    message: str | None
    trace: dict[str, Any]


@dataclass(frozen=True)
class RuleSetEvaluation:
    findings: tuple[RuleFinding, ...]
    derived: tuple[DerivedValue, ...]

    def triggered(self) -> tuple[RuleFinding, ...]:
        return tuple(f for f in self.findings if f.status == "triggered")

    def indeterminate(self) -> tuple[RuleFinding, ...]:
        return tuple(f for f in self.findings if f.status == "indeterminate")

    @property
    def blocking(self) -> bool:
        return any(f.action == "block" for f in self.triggered())

    @property
    def review_required(self) -> bool:
        return any(f.action in ("block", "route_to_review") for f in self.triggered())

    def summary(self) -> dict[str, Any]:
        counts = {severity: 0 for severity in sorted(SEVERITIES)}
        for finding in self.triggered():
            counts[finding.severity] += 1
        return {
            "rules_evaluated": len(self.findings),
            "triggered_by_severity": counts,
            "indeterminate": len(self.indeterminate()),
            "blocking": self.blocking,
            "review_required": self.review_required,
            "derived_values": len(self.derived),
        }


# -- value coercion -------------------------------------------------------------


def _is_money(value: Any) -> bool:
    return isinstance(value, Mapping) and "amount" in value


def _as_decimal(value: Any) -> Decimal | None:
    """Exact numeric reading of a canonical value; None if it has none.
    Floats are refused by design — canonical values are never floats."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    if _is_money(value):
        return _as_decimal(value["amount"])
    if isinstance(value, str):
        if len(value) > _MAX_LITERAL_LENGTH:
            return None
        try:
            return _DECIMAL_CONTEXT.create_decimal(value)
        except decimal.DecimalException:
            return None
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


# -- complexity ------------------------------------------------------------------


def _measure(node: Any, depth: int, limits: EvaluationLimits) -> int:
    """Count nodes and enforce depth; raises RuleComplexityError."""
    if depth > limits.max_depth:
        raise RuleComplexityError(f"expression exceeds the depth limit of {limits.max_depth}")
    if not isinstance(node, Mapping) or "op" not in node:
        raise RuleDefinitionError("expected an expression object with 'op'")
    count = 1
    for child_key in ("left", "right", "arg", "tolerance", "relative_tolerance"):
        if child_key in node:
            count += _measure(node[child_key], depth + 1, limits)
    if "args" in node:
        args = node["args"]
        if not isinstance(args, list):
            raise RuleDefinitionError("'args' must be a list")
        for arg in args:
            count += _measure(arg, depth + 1, limits)
    if count > limits.max_nodes_per_rule:
        raise RuleComplexityError(
            f"expression exceeds the node limit of {limits.max_nodes_per_rule}"
        )
    return count


# -- evaluation -------------------------------------------------------------------


class _Budget:
    def __init__(self, steps: int) -> None:
        self.remaining = steps

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise RuleComplexityError("rule-set evaluation exceeded its step budget")


class _Scope:
    """Value resolution for one evaluation pass (header or one row)."""

    def __init__(self, data: EvaluationInput, row: Mapping[str, Any] | None) -> None:
        self._data = data
        self._row = row

    def resolve(self, key: str) -> Any:
        if self._row is not None and key in self._row:
            return self._row[key]
        return self._data.header.get(key)

    def rows(self, table: str) -> Sequence[Mapping[str, Any]]:
        return self._data.tables.get(table, ())


def _compare(op: str, left: Any, right: Any) -> bool | None:
    """None = indeterminate (missing or incomparable operands)."""
    if left is None or right is None:
        return None
    left_num, right_num = _as_decimal(left), _as_decimal(right)
    if left_num is not None and right_num is not None:
        if _is_money(left) and _is_money(right):
            currencies = (left.get("currency"), right.get("currency"))
            if op in ("eq", "ne"):
                same = left_num == right_num and currencies[0] == currencies[1]
                return same if op == "eq" else not same
            if currencies[0] != currencies[1]:
                return None  # ordering money across currencies is meaningless
        left, right = left_num, right_num
    elif left_num is not None or right_num is not None:
        return None  # one numeric, one not: incomparable, never a silent False
    elif isinstance(left, bool) and isinstance(right, bool):
        if op not in ("eq", "ne"):
            return None
    elif not (isinstance(left, str) and isinstance(right, str)):
        return None  # mixed shapes: incomparable, never a silent False
    try:
        if op == "eq":
            return bool(left == right)
        if op == "ne":
            return bool(left != right)
        if op == "gt":
            return bool(left > right)
        if op == "gte":
            return bool(left >= right)
        if op == "lt":
            return bool(left < right)
        return bool(left <= right)
    except TypeError:
        return None


def _arithmetic(op: str, left: Any, right: Any) -> Decimal | None:
    left_num, right_num = _as_decimal(left), _as_decimal(right)
    if left_num is None or right_num is None:
        return None
    try:
        if op == "add":
            return _DECIMAL_CONTEXT.add(left_num, right_num)
        if op == "sub":
            return _DECIMAL_CONTEXT.subtract(left_num, right_num)
        if op == "mul":
            return _DECIMAL_CONTEXT.multiply(left_num, right_num)
        if right_num == 0:
            return None  # division by zero is indeterminate, never a crash
        return _DECIMAL_CONTEXT.divide(left_num, right_num)
    except decimal.DecimalException:
        return None  # overflow/invalid under the bounded context


def _eval(node: Mapping[str, Any], scope: _Scope, budget: _Budget) -> tuple[Any, dict[str, Any]]:
    """Returns (value, trace). The trace mirrors the expression with each
    node annotated by its resolved value; value None = indeterminate."""
    budget.spend()
    op = node.get("op")
    if op == "field":
        key = str(node.get("key"))
        value = scope.resolve(key)
        return value, {"op": "field", "key": key, "value": _json_safe(value)}
    if op == "const":
        value = node.get("value")
        return value, {"op": "const", "value": _json_safe(value)}
    if op == "is_present":
        key = str(node.get("key"))
        present = scope.resolve(key) is not None
        return present, {"op": "is_present", "key": key, "value": present}
    if op in COMPARISONS or op in ARITHMETIC or op == "approx_eq":
        left, left_trace = _eval(node["left"], scope, budget)
        right, right_trace = _eval(node["right"], scope, budget)
        trace: dict[str, Any] = {"op": op, "left": left_trace, "right": right_trace}
        if op in COMPARISONS:
            result: Any = _compare(op, left, right)
        elif op in ARITHMETIC:
            result = _arithmetic(op, left, right)
        else:
            result = _approx_eq(node, left, right, scope, budget, trace)
        if result is None:
            trace["indeterminate"] = True
        trace["result"] = _json_safe(result)
        return result, trace
    if op in ("and", "or"):
        results: list[Any] = []
        traces: list[dict[str, Any]] = []
        for arg in node["args"]:
            value, sub_trace = _eval(arg, scope, budget)
            results.append(value)
            traces.append(sub_trace)
        result = _kleene(op, results)
        return result, {"op": op, "args": traces, "result": _json_safe(result)}
    if op == "not":
        value, sub_trace = _eval(node["arg"], scope, budget)
        result = None if value is None else not bool(value)
        return result, {"op": "not", "arg": sub_trace, "result": _json_safe(result)}
    if op == "currency_of":
        value, sub_trace = _eval(node["arg"], scope, budget)
        # The currency a money value actually carries; indeterminate for
        # a bare amount (currency was never guessed) or a non-money value.
        currency = value.get("currency") if _is_money(value) else None
        trace = {"op": "currency_of", "arg": sub_trace, "result": currency}
        if currency is None:
            trace["indeterminate"] = True
        return currency, trace
    if op == "sum":
        return _sum(node, scope, budget)
    if op == "row_count":
        table = str(node.get("key"))
        count = len(scope.rows(table))
        return Decimal(count), {"op": "row_count", "key": table, "result": count}
    raise RuleDefinitionError(f"unknown op {op!r}")


def _kleene(op: str, results: list[Any]) -> bool | None:
    bools = [None if r is None else bool(r) for r in results]
    if op == "and":
        if any(b is False for b in bools):
            return False
        return None if any(b is None for b in bools) else True
    if any(b is True for b in bools):
        return True
    return None if any(b is None for b in bools) else False


def _sum(
    node: Mapping[str, Any], scope: _Scope, budget: _Budget
) -> tuple[Decimal | None, dict[str, Any]]:
    key = str(node.get("key"))
    table, _, _column = key.partition(".")
    rows = scope.rows(table)
    total = Decimal(0)
    missing_rows: list[int] = []
    for index, row in enumerate(rows):
        budget.spend()
        value = _as_decimal(row.get(key))
        if value is None:
            missing_rows.append(index)
            continue
        total = _DECIMAL_CONTEXT.add(total, value)
    trace: dict[str, Any] = {"op": "sum", "key": key, "rows": len(rows)}
    if missing_rows:
        # A sum over incomplete cells is unknown, not "the sum of the rest".
        trace["missing_rows"] = missing_rows
        trace["indeterminate"] = True
        trace["result"] = None
        return None, trace
    trace["result"] = str(total)
    return total, trace


def _approx_eq(
    node: Mapping[str, Any],
    left: Any,
    right: Any,
    scope: _Scope,
    budget: _Budget,
    trace: dict[str, Any],
) -> bool | None:
    """|left - right| <= tolerance + relative_tolerance * max(|l|, |r|).
    Both tolerances are explicit expressions (usually consts); omitted
    means zero — there is no hidden default slack."""
    left_num, right_num = _as_decimal(left), _as_decimal(right)
    if left_num is None or right_num is None:
        return None
    absolute = Decimal(0)
    relative = Decimal(0)
    if "tolerance" in node:
        value, tol_trace = _eval(node["tolerance"], scope, budget)
        trace["tolerance"] = tol_trace
        resolved = _as_decimal(value)
        if resolved is None:
            return None
        absolute = resolved
    if "relative_tolerance" in node:
        value, tol_trace = _eval(node["relative_tolerance"], scope, budget)
        trace["relative_tolerance"] = tol_trace
        resolved = _as_decimal(value)
        if resolved is None:
            return None
        relative = resolved
    try:
        allowed = _DECIMAL_CONTEXT.add(
            absolute,
            _DECIMAL_CONTEXT.multiply(relative, max(abs(left_num), abs(right_num))),
        )
        return bool(abs(_DECIMAL_CONTEXT.subtract(left_num, right_num)) <= allowed)
    except decimal.DecimalException:
        return None


# -- rule-set evaluation ------------------------------------------------------------


def evaluate_expression(
    condition: Mapping[str, Any],
    data: EvaluationInput,
    *,
    row: Mapping[str, Any] | None = None,
    limits: EvaluationLimits | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Evaluate one expression; returns (value, trace). Exposed for
    dry-runs and tests; evaluate_rule_set is the production entry."""
    effective = limits or EvaluationLimits()
    _measure(condition, 1, effective)
    return _eval(condition, _Scope(data, row), _Budget(effective.step_budget))


def _validate_rule(rule: Any, index: int) -> Mapping[str, Any]:
    path = f"rules[{index}]"
    if not isinstance(rule, Mapping):
        raise RuleDefinitionError(f"{path}: each rule must be an object")
    if not isinstance(rule.get("key"), str) or not rule["key"]:
        raise RuleDefinitionError(f"{path}: rule needs a non-empty 'key'")
    if rule.get("severity") not in SEVERITIES:
        raise RuleDefinitionError(f"{path}: invalid severity {rule.get('severity')!r}")
    if rule.get("action") not in ACTIONS:
        raise RuleDefinitionError(f"{path}: invalid action {rule.get('action')!r}")
    if not isinstance(rule.get("condition"), Mapping):
        raise RuleDefinitionError(f"{path}: missing condition expression")
    scope = rule.get("scope")
    if scope is not None and (not isinstance(scope, Mapping) or "table" not in scope):
        raise RuleDefinitionError(f"{path}: scope must be {{'table': <key>}}")
    return rule


def _status(value: Any) -> str:
    if value is None:
        return "indeterminate"
    return "triggered" if bool(value) else "passed"


def evaluate_rule_set(
    rules: Sequence[Any],
    data: EvaluationInput,
    *,
    limits: EvaluationLimits | None = None,
) -> RuleSetEvaluation:
    """Evaluate every rule (header rules once, row rules once per row of
    their table) in the given order. Deterministic: same rules + same
    values = same findings, traces, and derived values."""
    effective = limits or EvaluationLimits()
    if len(rules) > effective.max_rules:
        raise RuleComplexityError(f"rule set exceeds the limit of {effective.max_rules} rules")
    for table, rows in data.tables.items():
        if len(rows) > effective.max_rows_per_table:
            raise RuleComplexityError(
                f"table {table!r} exceeds the limit of {effective.max_rows_per_table} rows"
            )

    validated = [_validate_rule(rule, index) for index, rule in enumerate(rules)]
    for rule in validated:
        _measure(rule["condition"], 1, effective)
        for derivation in rule.get("derive", ()):
            if not isinstance(derivation, Mapping) or "field" not in derivation:
                raise RuleDefinitionError(
                    f"rule {rule['key']!r}: each derive entry needs 'field' and 'expression'"
                )
            _measure(derivation.get("expression"), 1, effective)

    budget = _Budget(effective.step_budget)
    findings: list[RuleFinding] = []
    derived: list[DerivedValue] = []

    for rule in validated:
        scope_spec = rule.get("scope")
        if scope_spec is None:
            passes: list[tuple[int | None, Mapping[str, Any] | None]] = [(None, None)]
        else:
            table = str(scope_spec["table"])
            passes = [(index, row) for index, row in enumerate(data.tables.get(table, ()))]
        for row_index, row in passes:
            scope = _Scope(data, row)
            value, trace = _eval(rule["condition"], scope, budget)
            findings.append(
                RuleFinding(
                    rule_key=str(rule["key"]),
                    severity=str(rule["severity"]),
                    action=str(rule["action"]),
                    status=_status(value),
                    row_index=row_index,
                    message=rule.get("message"),
                    trace=trace,
                )
            )
            for derivation in rule.get("derive", ()):
                target = str(derivation["field"])
                if scope.resolve(target) is not None:
                    continue  # never overwrite an extracted value
                derived_value, _derived_trace = _eval(derivation["expression"], scope, budget)
                if derived_value is None:
                    continue  # a derivation that cannot be computed derives nothing
                derived.append(
                    DerivedValue(
                        field_key=target,
                        row_index=row_index,
                        value=_json_safe(derived_value),
                    )
                )
    return RuleSetEvaluation(findings=tuple(findings), derived=tuple(derived))
