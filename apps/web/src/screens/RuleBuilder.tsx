/**
 * Rule builder (CFG-012, UI_UX_BLUEPRINT §6.5).
 *
 * Authors validation rules as typed condition trees over the process's
 * published schema fields — never free-text expressions. The generated
 * deterministic expression is always visible next to the builder, test
 * cases run server-side against the exact evaluator production uses, and
 * saves/publishes reuse the draft/publish lifecycle with If-Match
 * optimistic concurrency. The server re-validates everything: an
 * ill-typed rule set cannot be stored.
 */

import { Badge, Banner, Button, Checkbox, Select, TextField } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";

import {
  createRuleSetDraft,
  fetchRuleSet,
  publishRuleSetVersion,
  updateRuleSetDraft,
  validateRuleSet,
  type RuleCondition,
  type RuleRecord,
  type RuleSetVersionRecord,
  type RuleTestCase,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const SEVERITIES = ["error", "warning", "info"].map((id) => ({ id, label: id }));
const ACTIONS = [
  { id: "block", label: "Block export" },
  { id: "route_to_review", label: "Route to review" },
  { id: "annotate", label: "Annotate" },
];
const COMPARISON_OPS = [
  { id: "eq", label: "=" },
  { id: "ne", label: "≠" },
  { id: "gt", label: ">" },
  { id: "gte", label: "≥" },
  { id: "lt", label: "<" },
  { id: "lte", label: "≤" },
];
const CONDITION_KINDS = [
  { id: "comparison", label: "Comparison" },
  { id: "is_present", label: "Field is present" },
  { id: "and", label: "All of" },
  { id: "or", label: "Any of" },
  { id: "not", label: "Not" },
];

const OP_SYMBOLS: Record<string, string> = {
  eq: "=",
  ne: "≠",
  gt: ">",
  gte: "≥",
  lt: "<",
  lte: "≤",
};

/** Deterministic, readable rendering of the condition tree — what the
 * acceptance criterion calls the "generated deterministic expression". */
export function renderExpression(node: RuleCondition): string {
  switch (node.op) {
    case "field":
      return node.key || "?";
    case "const":
      return JSON.stringify(node.value);
    case "is_present":
      return `is_present(${node.key || "?"})`;
    case "and":
    case "or":
      return `(${node.args.map(renderExpression).join(node.op === "and" ? " and " : " or ")})`;
    case "not":
      return `not ${renderExpression(node.arg)}`;
    default:
      return `(${renderExpression(node.left)} ${OP_SYMBOLS[node.op]} ${renderExpression(node.right)})`;
  }
}

function kindOf(node: RuleCondition): string {
  if (node.op === "and" || node.op === "or" || node.op === "not" || node.op === "is_present") {
    return node.op;
  }
  return "comparison";
}

/** Coerce a text input to the field's declared type so number/money/boolean
 * comparisons type-check server-side. */
function coerceValue(raw: string, fieldType: string | undefined): string | number | boolean {
  if (fieldType === "number" || fieldType === "money") {
    const numeric = Number(raw);
    return Number.isNaN(numeric) ? raw : numeric;
  }
  if (fieldType === "boolean") return raw.trim() === "true";
  return raw;
}

function defaultCondition(fieldKeys: string[]): RuleCondition {
  return { op: "is_present", key: fieldKeys[0] ?? "" };
}

function ConditionEditor({
  node,
  fieldTypes,
  onChange,
  label,
}: {
  node: RuleCondition;
  fieldTypes: Record<string, string>;
  onChange: (next: RuleCondition) => void;
  label: string;
}) {
  const fieldKeys = Object.keys(fieldTypes);
  const fieldItems = fieldKeys.map((id) => ({ id, label: id }));
  const kind = kindOf(node);

  const switchKind = (nextKind: string) => {
    if (nextKind === kind) return;
    const first = fieldKeys[0] ?? "";
    if (nextKind === "comparison") {
      onChange({ op: "eq", left: { op: "field", key: first }, right: { op: "const", value: "" } });
    } else if (nextKind === "is_present") {
      onChange({ op: "is_present", key: first });
    } else if (nextKind === "not") {
      onChange({ op: "not", arg: defaultCondition(fieldKeys) });
    } else {
      onChange({
        op: nextKind as "and" | "or",
        args: [defaultCondition(fieldKeys), defaultCondition(fieldKeys)],
      });
    }
  };

  return (
    <div
      style={{
        display: "grid",
        gap: "var(--soa-space-2)",
        padding: "var(--soa-space-2)",
        border: "1px dashed var(--soa-border)",
        borderRadius: "var(--soa-radius-control)",
      }}
    >
      <div
        style={{ display: "flex", gap: "var(--soa-space-3)", alignItems: "end", flexWrap: "wrap" }}
      >
        <Select
          label={label}
          items={CONDITION_KINDS}
          selectedKey={kind}
          onSelectionChange={(key) => switchKind(String(key))}
        />
        {node.op === "is_present" ? (
          <Select
            label="Field"
            items={fieldItems}
            selectedKey={node.key}
            onSelectionChange={(key) => onChange({ op: "is_present", key: String(key) })}
          />
        ) : null}
        {kind === "comparison" && "left" in node ? (
          <ComparisonEditor node={node} fieldTypes={fieldTypes} onChange={onChange} />
        ) : null}
      </div>
      {node.op === "not" ? (
        <div style={{ marginLeft: "var(--soa-space-5)" }}>
          <ConditionEditor
            node={node.arg}
            fieldTypes={fieldTypes}
            onChange={(arg) => onChange({ op: "not", arg })}
            label="Condition"
          />
        </div>
      ) : null}
      {node.op === "and" || node.op === "or" ? (
        <div
          style={{ marginLeft: "var(--soa-space-5)", display: "grid", gap: "var(--soa-space-2)" }}
        >
          {node.args.map((arg, index) => (
            <div key={index} style={{ display: "grid", gap: "var(--soa-space-1)" }}>
              <ConditionEditor
                node={arg}
                fieldTypes={fieldTypes}
                onChange={(next) =>
                  onChange({ ...node, args: node.args.map((a, i) => (i === index ? next : a)) })
                }
                label={`Condition ${index + 1}`}
              />
              {node.args.length > 2 ? (
                <div>
                  <Button
                    size="sm"
                    variant="subtle"
                    onPress={() =>
                      onChange({ ...node, args: node.args.filter((_, i) => i !== index) })
                    }
                  >
                    Remove condition {index + 1}
                  </Button>
                </div>
              ) : null}
            </div>
          ))}
          <div>
            <Button
              size="sm"
              variant="subtle"
              onPress={() =>
                onChange({
                  ...node,
                  args: [...node.args, defaultCondition(Object.keys(fieldTypes))],
                })
              }
            >
              Add condition
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ComparisonEditor({
  node,
  fieldTypes,
  onChange,
}: {
  node: Extract<RuleCondition, { op: "eq" | "ne" | "gt" | "gte" | "lt" | "lte" }>;
  fieldTypes: Record<string, string>;
  onChange: (next: RuleCondition) => void;
}) {
  const fieldItems = Object.keys(fieldTypes).map((id) => ({ id, label: id }));
  const leftKey = node.left.op === "field" ? node.left.key : "";
  const rightIsField = node.right.op === "field";
  const constValue = node.right.op === "const" ? node.right.value : "";
  return (
    <>
      <Select
        label="Field"
        items={fieldItems}
        selectedKey={leftKey}
        onSelectionChange={(key) => onChange({ ...node, left: { op: "field", key: String(key) } })}
      />
      <Select
        label="Operator"
        items={COMPARISON_OPS}
        selectedKey={node.op}
        onSelectionChange={(key) => onChange({ ...node, op: String(key) as typeof node.op })}
      />
      <Select
        label="Compare with"
        items={[
          { id: "value", label: "a value" },
          { id: "field", label: "another field" },
        ]}
        selectedKey={rightIsField ? "field" : "value"}
        onSelectionChange={(key) =>
          onChange({
            ...node,
            right:
              key === "field"
                ? { op: "field", key: Object.keys(fieldTypes)[0] ?? "" }
                : { op: "const", value: "" },
          })
        }
      />
      {rightIsField ? (
        <Select
          label="Other field"
          items={fieldItems}
          selectedKey={node.right.op === "field" ? node.right.key : ""}
          onSelectionChange={(key) =>
            onChange({ ...node, right: { op: "field", key: String(key) } })
          }
        />
      ) : (
        <TextField
          label="Value"
          value={String(constValue)}
          onChange={(raw) =>
            onChange({
              ...node,
              right: { op: "const", value: coerceValue(raw, fieldTypes[leftKey]) },
            })
          }
        />
      )}
    </>
  );
}

function TestCaseEditor({
  testCase,
  index,
  fieldTypes,
  onChange,
  onRemove,
}: {
  testCase: RuleTestCase;
  index: number;
  fieldTypes: Record<string, string>;
  onChange: (next: RuleTestCase) => void;
  onRemove: () => void;
}) {
  const fieldItems = Object.keys(fieldTypes).map((id) => ({ id, label: id }));
  const entries = Object.entries(testCase.values);
  return (
    <div
      style={{
        display: "grid",
        gap: "var(--soa-space-2)",
        padding: "var(--soa-space-2)",
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-control)",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: "var(--soa-space-3)",
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <strong>Test case {index + 1}</strong>
        <Checkbox
          isSelected={testCase.expect_triggered}
          onChange={(expect_triggered) => onChange({ ...testCase, expect_triggered })}
        >
          Expect the rule to trigger
        </Checkbox>
        <Button
          size="sm"
          variant="subtle"
          aria-label={`Remove test case ${index + 1}`}
          onPress={onRemove}
        >
          Remove
        </Button>
      </div>
      {entries.map(([key, value], entryIndex) => (
        <div
          key={entryIndex}
          style={{
            display: "flex",
            gap: "var(--soa-space-3)",
            alignItems: "end",
            flexWrap: "wrap",
          }}
        >
          <Select
            label={`Field for value ${entryIndex + 1}`}
            items={fieldItems}
            selectedKey={key}
            onSelectionChange={(nextKey) => {
              const values = Object.fromEntries(
                entries.map(([k, v], i) => (i === entryIndex ? [String(nextKey), v] : [k, v])),
              );
              onChange({ ...testCase, values });
            }}
          />
          <TextField
            label={`Value ${entryIndex + 1}`}
            value={value === null || value === undefined ? "" : String(value)}
            onChange={(raw) =>
              onChange({
                ...testCase,
                values: { ...testCase.values, [key]: coerceValue(raw, fieldTypes[key]) },
              })
            }
          />
        </div>
      ))}
      <div>
        <Button
          size="sm"
          variant="subtle"
          onPress={() => {
            const unused = Object.keys(fieldTypes).find((k) => !(k in testCase.values));
            if (!unused) return;
            onChange({ ...testCase, values: { ...testCase.values, [unused]: "" } });
          }}
        >
          Add value
        </Button>
      </div>
    </div>
  );
}

function RuleEditor({
  rule,
  fieldTypes,
  onChange,
  onRemove,
}: {
  rule: RuleRecord;
  fieldTypes: Record<string, string>;
  onChange: (next: RuleRecord) => void;
  onRemove: () => void;
}) {
  const cases = rule.test_cases ?? [];
  return (
    <div
      style={{
        display: "grid",
        gap: "var(--soa-space-3)",
        padding: "var(--soa-space-4)",
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
      }}
    >
      <div
        style={{ display: "flex", gap: "var(--soa-space-3)", alignItems: "end", flexWrap: "wrap" }}
      >
        <TextField
          label="Rule key"
          value={rule.key}
          onChange={(key) => onChange({ ...rule, key })}
        />
        <Select
          label="Severity"
          items={SEVERITIES}
          selectedKey={rule.severity}
          onSelectionChange={(key) =>
            onChange({ ...rule, severity: String(key) as RuleRecord["severity"] })
          }
        />
        <Select
          label="Action"
          items={ACTIONS}
          selectedKey={rule.action}
          onSelectionChange={(key) =>
            onChange({ ...rule, action: String(key) as RuleRecord["action"] })
          }
        />
        <Button
          size="sm"
          variant="destructive"
          aria-label={`Remove rule ${rule.key || "unnamed"}`}
          onPress={onRemove}
        >
          Remove rule
        </Button>
      </div>

      <ConditionEditor
        node={rule.condition}
        fieldTypes={fieldTypes}
        onChange={(condition) => onChange({ ...rule, condition })}
        label="When"
      />

      <div aria-label={`Expression for ${rule.key || "unnamed"}`} role="note">
        <code>{renderExpression(rule.condition)}</code>
      </div>

      <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
        {cases.map((testCase, index) => (
          <TestCaseEditor
            key={index}
            testCase={testCase}
            index={index}
            fieldTypes={fieldTypes}
            onChange={(next) =>
              onChange({ ...rule, test_cases: cases.map((c, i) => (i === index ? next : c)) })
            }
            onRemove={() => onChange({ ...rule, test_cases: cases.filter((_, i) => i !== index) })}
          />
        ))}
        <div>
          <Button
            size="sm"
            variant="subtle"
            onPress={() =>
              onChange({
                ...rule,
                test_cases: [...cases, { values: {}, expect_triggered: true }],
              })
            }
          >
            Add test case
          </Button>
        </div>
      </div>
    </div>
  );
}

export function RuleBuilder() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("processes.manage");
  const { processSlug } = useParams({ strict: false }) as { processSlug: string };
  const queryClient = useQueryClient();

  const listing = useQuery({
    queryKey: ["rules", slug, processSlug],
    queryFn: () => fetchRuleSet(slug, processSlug),
  });

  const versions: RuleSetVersionRecord[] = listing.data?.versions ?? [];
  const fieldTypes = useMemo(() => listing.data?.field_types ?? {}, [listing.data]);
  const draft = [...versions].reverse().find((v) => v.state === "draft");
  const published = [...versions].reverse().find((v) => v.state === "published");
  const baseline = useMemo(
    () => draft?.definition ?? published?.definition ?? { rules: [] },
    [draft, published],
  );

  const [rules, setRules] = useState<RuleRecord[]>(baseline.rules);
  useEffect(() => setRules(baseline.rules), [baseline]);
  const dirty = JSON.stringify(rules) !== JSON.stringify(baseline.rules);

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["rules", slug, processSlug] });
  const save = useMutation({
    mutationFn: () =>
      draft
        ? updateRuleSetDraft(slug, processSlug, draft.id, draft.version, { rules })
        : createRuleSetDraft(slug, processSlug, { rules }),
    onSuccess: invalidate,
  });
  const publish = useMutation({
    mutationFn: (versionId: string) => publishRuleSetVersion(slug, processSlug, versionId),
    onSuccess: invalidate,
  });
  const dryRun = useMutation({
    mutationFn: () => validateRuleSet(slug, processSlug, { rules }),
  });

  const noSchema = listing.data !== undefined && Object.keys(fieldTypes).length === 0;

  if (listing.status === "error") {
    return (
      <AppShell title="Rules" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load the rules"
          action={
            <Button size="sm" onPress={() => void listing.refetch()}>
              Try again
            </Button>
          }
        >
          Nothing has been changed.
        </Banner>
      </AppShell>
    );
  }

  return (
    <AppShell
      title="Validation rules"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Processes", to: "/app/$organizationSlug/processes" },
        { label: processSlug },
        { label: "Rules" },
      ]}
      actions={
        canManage ? (
          <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "center" }}>
            {dirty ? <Badge tone="warning">Unsaved changes</Badge> : null}
            <Button variant="subtle" onPress={() => dryRun.mutate()} isDisabled={dryRun.isPending}>
              Run test cases
            </Button>
            <Button onPress={() => save.mutate()} isDisabled={!dirty || save.isPending}>
              {draft ? "Save draft" : "Create draft"}
            </Button>
            {draft ? (
              <Button
                variant="secondary"
                isDisabled={dirty || publish.isPending}
                onPress={() => publish.mutate(draft.id)}
              >
                Publish
              </Button>
            ) : null}
          </div>
        ) : undefined
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        {noSchema ? (
          <Banner tone="warning" title="No published schema yet">
            Rules type-check against the process’s published extraction schema. Publish a schema
            first — until then any field reference will fail validation.
          </Banner>
        ) : null}
        {save.isError ? (
          <Banner tone="critical" title="The rules were not saved">
            {save.error?.message ?? "Validation failed."}
          </Banner>
        ) : null}
        {publish.isError ? (
          <Banner tone="critical" title="Publish refused">
            {publish.error?.message ?? "The draft did not validate."}
          </Banner>
        ) : null}
        {dryRun.data ? (
          dryRun.data.valid ? (
            <Banner tone="success" title="All test cases pass">
              Every condition type-checks and every authored test case agrees with the evaluator.
            </Banner>
          ) : (
            <Banner tone="critical" title="Validation failed">
              {dryRun.data.message}
            </Banner>
          )
        ) : null}
        {published && !draft && !dirty ? (
          <Banner tone="info" title={`Editing starts from published v${published.version_number}`}>
            Your first change creates a new draft; the published version stays untouched.
          </Banner>
        ) : null}

        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          {rules.map((rule, index) => (
            <RuleEditor
              key={index}
              rule={rule}
              fieldTypes={fieldTypes}
              onChange={(next) =>
                setRules((current) => current.map((r, i) => (i === index ? next : r)))
              }
              onRemove={() => setRules((current) => current.filter((_, i) => i !== index))}
            />
          ))}
          <div>
            <Button
              onPress={() =>
                setRules((current) => [
                  ...current,
                  {
                    key: "",
                    severity: "warning",
                    action: "route_to_review",
                    condition: defaultCondition(Object.keys(fieldTypes)),
                    test_cases: [],
                  },
                ])
              }
            >
              Add rule
            </Button>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
