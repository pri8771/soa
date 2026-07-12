/**
 * URL-synchronized table state (DSN-006): sort, density, and cursor live in
 * search params so filtered views are addressable and shareable
 * (UI_UX_BLUEPRINT §6.3). Saved views (ING-010+) persist these same params.
 */

import { useNavigate, useSearch } from "@tanstack/react-router";
import type { SortingState } from "@tanstack/react-table";

export interface TableUrlState {
  sorting: SortingState;
  density: "dense" | "comfortable";
  cursor: string | null;
  setSorting: (sorting: SortingState) => void;
  setDensity: (density: "dense" | "comfortable") => void;
  setCursor: (cursor: string | null) => void;
}

interface RawSearch {
  sort?: string;
  density?: string;
  cursor?: string;
}

export function useTableUrlState(): TableUrlState {
  const search = useSearch({ strict: false }) as RawSearch;
  const navigate = useNavigate();

  const sorting: SortingState = search.sort
    ? [
        {
          id: search.sort.replace(/^-/, ""),
          desc: search.sort.startsWith("-"),
        },
      ]
    : [];
  const density = search.density === "comfortable" ? "comfortable" : "dense";
  const cursor = search.cursor ?? null;

  function patch(update: Partial<RawSearch>) {
    void navigate({
      // Merge with existing params; undefined removes a key.
      search: (previous: Record<string, unknown>) => {
        const next: Record<string, unknown> = { ...previous, ...update };
        for (const key of Object.keys(next)) {
          if (next[key] === null || next[key] === undefined || next[key] === "") {
            delete next[key];
          }
        }
        return next;
      },
      replace: true,
    } as never);
  }

  return {
    sorting,
    density,
    cursor,
    setSorting: (next) => {
      const entry = next[0];
      patch({
        sort: entry ? `${entry.desc ? "-" : ""}${entry.id}` : undefined,
        cursor: undefined,
      });
    },
    setDensity: (next) => patch({ density: next === "dense" ? undefined : next }),
    setCursor: (next) => patch({ cursor: next ?? undefined }),
  };
}
