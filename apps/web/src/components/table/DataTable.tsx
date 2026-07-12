/**
 * Data-table system (DSN-006, UI_UX_BLUEPRINT §6.3).
 *
 * Server-oriented: sorting/pagination state lives with the caller (synced
 * to the URL via useTableUrlState) and every change is a callback — the
 * table never sorts or filters client-side data behind the API's back.
 * Rows virtualize past ``virtualizeAt`` so realistic queue volumes scroll
 * smoothly. Semantics: real <table>, scope=col headers, aria-sort, a
 * caption for assistive technology, selection column with labeled
 * checkboxes, and a bulk-action bar that announces the selection count.
 */

import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef, type ReactNode } from "react";

import { Button, Skeleton } from "@soa/design-system";

import "./table.css";

export type TableStatus = "loading" | "error" | "ready";

export interface DataTableProps<T> {
  caption: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- TanStack column defs are heterogeneous by design
  columns: ColumnDef<T, any>[];
  data: T[];
  getRowId: (row: T) => string;
  status?: TableStatus;
  errorMessage?: string;
  onRetry?: () => void;
  emptyTitle?: string;
  emptyBody?: string;
  emptyAction?: ReactNode;
  sorting?: SortingState;
  onSortingChange?: (sorting: SortingState) => void;
  selectedIds?: ReadonlySet<string>;
  onSelectionChange?: (ids: Set<string>) => void;
  bulkActions?: ReactNode;
  density?: "dense" | "comfortable";
  stickyFirstColumn?: boolean;
  /** Virtualize row rendering above this row count. */
  virtualizeAt?: number;
  hasMore?: boolean;
  onLoadMore?: () => void;
  onRowActivate?: (row: T) => void;
}

const SKELETON_ROWS = 8;

export function DataTable<T>({
  caption,
  columns,
  data,
  getRowId,
  status = "ready",
  errorMessage,
  onRetry,
  emptyTitle = "Nothing here yet",
  emptyBody = "Records appear here as they arrive.",
  emptyAction,
  sorting = [],
  onSortingChange,
  selectedIds,
  onSelectionChange,
  bulkActions,
  density = "dense",
  stickyFirstColumn = false,
  virtualizeAt = 100,
  hasMore = false,
  onLoadMore,
  onRowActivate,
}: DataTableProps<T>) {
  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
    manualSorting: true,
    state: { sorting },
    getRowId,
  });

  const rows = table.getRowModel().rows;
  const scrollRef = useRef<HTMLDivElement>(null);
  const shouldVirtualize = rows.length > virtualizeAt;
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => (density === "dense" ? 38 : 46),
    overscan: 12,
    enabled: shouldVirtualize,
  });

  const selectable = selectedIds !== undefined && onSelectionChange !== undefined;
  const allIds = rows.map((row) => row.id);
  const allSelected = selectable && allIds.length > 0 && allIds.every((id) => selectedIds.has(id));

  function toggleAll() {
    if (!selectable) return;
    onSelectionChange(allSelected ? new Set() : new Set(allIds));
  }

  function toggleRow(id: string) {
    if (!selectable) return;
    const next = new Set(selectedIds);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    onSelectionChange(next);
  }

  function handleSortClick(columnId: string, canSort: boolean) {
    if (!canSort || !onSortingChange) return;
    const current = sorting.find((entry) => entry.id === columnId);
    if (!current) {
      onSortingChange([{ id: columnId, desc: false }]);
    } else if (!current.desc) {
      onSortingChange([{ id: columnId, desc: true }]);
    } else {
      onSortingChange([]);
    }
  }

  if (status === "loading") {
    return (
      <div className="soa-table-frame" data-density={density}>
        <table className="soa-table">
          <caption className="soa-sr-only">{caption} (loading)</caption>
          <thead>
            <tr>
              {columns.map((_column, index) => (
                <th key={index} scope="col">
                  <Skeleton width="80px" height="0.9rem" />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: SKELETON_ROWS }, (_, rowIndex) => (
              <tr key={rowIndex}>
                {columns.map((_, columnIndex) => (
                  <td key={columnIndex}>
                    <Skeleton height="0.9rem" />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="soa-table-state" role="alert">
        <p className="soa-table-state-title">Couldn’t load this list</p>
        <p className="soa-table-state-body">
          {errorMessage ?? "The request failed. Your data is unchanged."}
        </p>
        {onRetry ? (
          <Button variant="secondary" onPress={onRetry}>
            Try again
          </Button>
        ) : null}
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="soa-table-state">
        <p className="soa-table-state-title">{emptyTitle}</p>
        <p className="soa-table-state-body">{emptyBody}</p>
        {emptyAction}
      </div>
    );
  }

  const virtualRows = shouldVirtualize ? virtualizer.getVirtualItems() : null;
  const paddingTop = virtualRows && virtualRows.length > 0 ? virtualRows[0].start : 0;
  const paddingBottom =
    virtualRows && virtualRows.length > 0
      ? virtualizer.getTotalSize() - virtualRows[virtualRows.length - 1].end
      : 0;
  const renderedRows = virtualRows ? virtualRows.map((virtual) => rows[virtual.index]) : rows;

  return (
    <div className="soa-table-system">
      {selectable && selectedIds.size > 0 ? (
        <div className="soa-table-bulkbar" role="toolbar" aria-label="Bulk actions">
          <span aria-live="polite" className="soa-table-bulkbar-count">
            {selectedIds.size} selected
          </span>
          {bulkActions}
          <Button variant="subtle" size="sm" onPress={() => onSelectionChange(new Set())}>
            Clear selection
          </Button>
        </div>
      ) : null}

      <div
        className="soa-table-frame"
        data-density={density}
        data-sticky-first={stickyFirstColumn}
        ref={scrollRef}
        style={shouldVirtualize ? { maxHeight: 560, overflow: "auto" } : undefined}
      >
        <table className="soa-table">
          <caption className="soa-sr-only">{caption}</caption>
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {selectable ? (
                  <th scope="col" className="soa-table-select-cell">
                    <input
                      type="checkbox"
                      aria-label="Select all rows"
                      checked={allSelected}
                      onChange={toggleAll}
                    />
                  </th>
                ) : null}
                {headerGroup.headers.map((header) => {
                  const canSort = header.column.columnDef.enableSorting !== false;
                  const sortEntry = sorting.find((entry) => entry.id === header.column.id);
                  const ariaSort = sortEntry
                    ? sortEntry.desc
                      ? "descending"
                      : "ascending"
                    : undefined;
                  return (
                    <th key={header.id} scope="col" aria-sort={ariaSort}>
                      {onSortingChange && canSort ? (
                        <button
                          type="button"
                          className="soa-table-sort-button"
                          onClick={() => handleSortClick(header.column.id, canSort)}
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          <span aria-hidden="true" className="soa-table-sort-marker">
                            {sortEntry ? (sortEntry.desc ? "↓" : "↑") : "↕"}
                          </span>
                        </button>
                      ) : (
                        flexRender(header.column.columnDef.header, header.getContext())
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {paddingTop > 0 ? (
              <tr aria-hidden="true" style={{ height: paddingTop }}>
                <td colSpan={columns.length + (selectable ? 1 : 0)} />
              </tr>
            ) : null}
            {renderedRows.map((row) => (
              <tr
                key={row.id}
                data-selected={selectable && selectedIds.has(row.id)}
                onClick={onRowActivate ? () => onRowActivate(row.original) : undefined}
                onKeyDown={
                  onRowActivate
                    ? (event) => {
                        if (event.key === "Enter") {
                          onRowActivate(row.original);
                        }
                      }
                    : undefined
                }
                tabIndex={onRowActivate ? 0 : undefined}
              >
                {selectable ? (
                  <td
                    className="soa-table-select-cell"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <input
                      type="checkbox"
                      aria-label={`Select row ${row.id}`}
                      checked={selectedIds.has(row.id)}
                      onChange={() => toggleRow(row.id)}
                    />
                  </td>
                ) : null}
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                ))}
              </tr>
            ))}
            {paddingBottom > 0 ? (
              <tr aria-hidden="true" style={{ height: paddingBottom }}>
                <td colSpan={columns.length + (selectable ? 1 : 0)} />
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      {hasMore && onLoadMore ? (
        <div className="soa-table-footer">
          <Button variant="secondary" size="sm" onPress={onLoadMore}>
            Load more
          </Button>
        </div>
      ) : null}
    </div>
  );
}
