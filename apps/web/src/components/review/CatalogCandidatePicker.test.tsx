import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CatalogCandidatePicker, type CatalogCandidate } from "./CatalogCandidatePicker";

const CANDIDATES: CatalogCandidate[] = [
  {
    id: "cat-1",
    code: "WID-100",
    label: "Widget, 10mm galvanized",
    score: 0.92,
    features: [
      { name: "code similarity", score: 0.95, explanation: "exact prefix match on WID" },
      { name: "description overlap", score: 0.88, explanation: "shares 'widget' and '10mm'" },
    ],
  },
  {
    id: "cat-2",
    code: "WID-101",
    label: "Widget, 12mm galvanized",
    score: 0.71,
    features: [{ name: "code similarity", score: 0.7, explanation: "one digit differs" }],
  },
];

function renderPicker(options?: {
  loader?: (query: string) => Promise<CatalogCandidate[]>;
  onPick?: (candidate: CatalogCandidate, reason: string | null) => void;
}) {
  return render(
    <CatalogCandidatePicker
      fieldLabel="sku"
      initialQuery="WID-100"
      loadCandidates={options?.loader ?? (() => Promise.resolve(CANDIDATES))}
      onPick={options?.onPick ?? (() => undefined)}
    />,
  );
}

describe("Catalog candidate picker shell (REV-010)", () => {
  it("ranks candidates with scores and expandable feature explanations", async () => {
    const user = userEvent.setup();
    renderPicker();
    await user.click(screen.getByRole("button", { name: "Search catalog" }));
    const list = await screen.findByRole("list", { name: "Catalog candidates" });
    const [best, second] = within(list).getAllByRole("listitem");
    expect(within(best).getByText("WID-100")).toBeInTheDocument();
    expect(within(best).getByText("92% match")).toBeInTheDocument();
    expect(within(best).getByText("best match")).toBeInTheDocument();
    expect(within(second).getByText("71% match")).toBeInTheDocument();

    // Feature scores and explanations are one click away.
    await user.click(within(best).getByRole("button", { name: "Why this score" }));
    const explanation = within(best).getByRole("list", {
      name: "Match explanation for WID-100",
    });
    expect(
      within(explanation).getByText(/code similarity: 95% — exact prefix match on WID/),
    ).toBeInTheDocument();
  });

  it("picking the best match needs no override; anything else needs a reason", async () => {
    const user = userEvent.setup();
    const picks: [string, string | null][] = [];
    renderPicker({ onPick: (candidate, reason) => picks.push([candidate.code, reason]) });
    await user.click(screen.getByRole("button", { name: "Search catalog" }));
    await screen.findByRole("list", { name: "Catalog candidates" });

    await user.click(screen.getByRole("button", { name: "Pick WID-100" }));
    expect(picks).toEqual([["WID-100", null]]);

    // The runner-up: a manual override with a mandatory reason.
    await user.click(screen.getByRole("button", { name: "Pick WID-101" }));
    const override = await screen.findByRole("group", { name: "Manual override" });
    const confirm = within(override).getByRole("button", { name: "Pick with override" });
    expect(confirm).toBeDisabled();
    await user.type(within(override).getByLabelText(/Override reason/), "vendor renamed the part");
    await user.click(confirm);
    expect(picks[1]).toEqual(["WID-101", "vendor renamed the part"]);
  });

  it("shows the loading state while the catalog is queried", async () => {
    const user = userEvent.setup();
    let resolve: (candidates: CatalogCandidate[]) => void = () => undefined;
    renderPicker({
      loader: () =>
        new Promise<CatalogCandidate[]>((r) => {
          resolve = r;
        }),
    });
    await user.click(screen.getByRole("button", { name: "Search catalog" }));
    expect(screen.queryByRole("list", { name: "Catalog candidates" })).not.toBeInTheDocument();
    resolve(CANDIDATES);
    await waitFor(() =>
      expect(screen.getByRole("list", { name: "Catalog candidates" })).toBeInTheDocument(),
    );
  });

  it("says honestly when nothing matches", async () => {
    const user = userEvent.setup();
    renderPicker({ loader: () => Promise.resolve([]) });
    await user.click(screen.getByRole("button", { name: "Search catalog" }));
    expect(await screen.findByText("No catalog match")).toBeInTheDocument();
    expect(screen.getByText(/Keep the document value/)).toBeInTheDocument();
  });

  it("surfaces catalog errors with a retry", async () => {
    const user = userEvent.setup();
    let failures = 1;
    renderPicker({
      loader: () =>
        failures-- > 0
          ? Promise.reject(new Error("catalog service unavailable"))
          : Promise.resolve(CANDIDATES),
    });
    await user.click(screen.getByRole("button", { name: "Search catalog" }));
    expect(await screen.findByText("Catalog lookup failed")).toBeInTheDocument();
    expect(screen.getByText("catalog service unavailable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Catalog candidates" })).toBeInTheDocument();
  });
});
