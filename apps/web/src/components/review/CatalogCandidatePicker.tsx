/**
 * Catalog candidate picker shell (REV-010, UI_UX_BLUEPRINT §5.9).
 *
 * The interaction contract for matching a document value against the
 * stream's customer catalog. The live matcher is supplied through the
 * injected ``loadCandidates`` callback; this component owns a search box, ranked candidates
 * with their match score and per-feature explanation (expandable), a
 * pick action, and a MANUAL OVERRIDE path that requires a stated reason
 * whenever the reviewer picks something other than the top match (or no
 * match at all). Loading, no-match, and error states are explicit.
 *
 * Injection keeps the interaction independently testable and the picker
 * decoupled from API transport details.
 */

import { Badge, Banner, Button, Skeleton, TextField } from "@soa/design-system";
import { useState } from "react";

export interface CandidateFeature {
  name: string;
  score: number;
  explanation: string;
}

export interface CatalogCandidate {
  id: string;
  code: string;
  label: string;
  /** Overall match score in [0, 1]. */
  score: number;
  features: CandidateFeature[];
}

export type CandidateLoader = (query: string) => Promise<CatalogCandidate[]>;

type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "loaded"; query: string; candidates: CatalogCandidate[] }
  | { status: "error"; message: string };

export function CatalogCandidatePicker({
  fieldLabel,
  initialQuery,
  loadCandidates,
  onPick,
}: {
  fieldLabel: string;
  initialQuery: string;
  loadCandidates: CandidateLoader;
  /** reason is present when the pick is a manual override. */
  onPick: (candidate: CatalogCandidate, reason: string | null) => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [state, setState] = useState<LoadState>({ status: "idle" });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [overriding, setOverriding] = useState<CatalogCandidate | null>(null);
  const [overrideReason, setOverrideReason] = useState("");

  const search = async () => {
    setState({ status: "loading" });
    setOverriding(null);
    try {
      const candidates = await loadCandidates(query.trim());
      setState({ status: "loaded", query: query.trim(), candidates });
    } catch (error) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "The catalog did not respond.",
      });
    }
  };

  const topId = state.status === "loaded" ? state.candidates[0]?.id : undefined;

  return (
    <section
      aria-label={`Catalog match for ${fieldLabel}`}
      data-catalog-picker
      style={{ display: "grid", gap: "var(--soa-space-3)" }}
    >
      <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "end" }}>
        <TextField label={`Search catalog for ${fieldLabel}`} value={query} onChange={setQuery} />
        <Button size="sm" variant="secondary" onPress={() => void search()}>
          Search catalog
        </Button>
      </div>

      {state.status === "loading" ? <Skeleton height="6rem" /> : null}
      {state.status === "error" ? (
        <Banner
          tone="critical"
          title="Catalog lookup failed"
          action={
            <Button size="sm" onPress={() => void search()}>
              Try again
            </Button>
          }
        >
          {state.message}
        </Banner>
      ) : null}
      {state.status === "loaded" && state.candidates.length === 0 ? (
        <Banner tone="info" title="No catalog match">
          Nothing in the catalog matches “{state.query}”. Keep the document value, or search with
          different terms.
        </Banner>
      ) : null}

      {state.status === "loaded" && state.candidates.length > 0 ? (
        <ol
          aria-label="Catalog candidates"
          style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}
        >
          {state.candidates.map((candidate) => (
            <li
              key={candidate.id}
              style={{
                border: "1px solid var(--soa-border)",
                borderRadius: "var(--soa-radius-panel)",
                padding: "var(--soa-space-3)",
                display: "grid",
                gap: "0.375rem",
              }}
            >
              <div
                style={{
                  display: "flex",
                  gap: "var(--soa-space-2)",
                  alignItems: "baseline",
                  flexWrap: "wrap",
                }}
              >
                <strong>{candidate.code}</strong>
                <span>{candidate.label}</span>
                <Badge tone={candidate.score >= 0.8 ? "success" : "warning"}>
                  {Math.round(candidate.score * 100)}% match
                </Badge>
                {candidate.id === topId ? <Badge tone="accent">best match</Badge> : null}
                <Button
                  size="sm"
                  variant="subtle"
                  aria-expanded={expanded === candidate.id}
                  onPress={() =>
                    setExpanded((current) => (current === candidate.id ? null : candidate.id))
                  }
                >
                  Why this score
                </Button>
                <Button
                  size="sm"
                  variant={candidate.id === topId ? "primary" : "secondary"}
                  aria-label={`Pick ${candidate.code}`}
                  onPress={() => {
                    if (candidate.id === topId) {
                      onPick(candidate, null);
                    } else {
                      // Anything but the best match is a manual override:
                      // it needs a stated reason.
                      setOverriding(candidate);
                      setOverrideReason("");
                    }
                  }}
                >
                  Pick
                </Button>
              </div>
              {expanded === candidate.id ? (
                <ul
                  aria-label={`Match explanation for ${candidate.code}`}
                  style={{ margin: 0, paddingLeft: "1.25rem", font: "var(--soa-font-caption)" }}
                >
                  {candidate.features.map((feature) => (
                    <li key={feature.name}>
                      {feature.name}: {Math.round(feature.score * 100)}% — {feature.explanation}
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}

      {overriding ? (
        <div
          role="group"
          aria-label="Manual override"
          style={{
            border: "1px solid var(--soa-border)",
            borderRadius: "var(--soa-radius-panel)",
            padding: "var(--soa-space-3)",
            display: "grid",
            gap: "var(--soa-space-2)",
          }}
        >
          <p style={{ margin: 0 }}>
            {overriding.code} is not the best match — overriding the ranking needs a reason, which
            is recorded with the correction.
          </p>
          <TextField
            label="Override reason (required)"
            value={overrideReason}
            onChange={setOverrideReason}
            isRequired
          />
          <div style={{ display: "flex", gap: "var(--soa-space-2)" }}>
            <Button size="sm" variant="subtle" onPress={() => setOverriding(null)}>
              Cancel
            </Button>
            <Button
              size="sm"
              isDisabled={overrideReason.trim().length < 3}
              onPress={() => {
                onPick(overriding, overrideReason.trim());
                setOverriding(null);
              }}
            >
              Pick with override
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
