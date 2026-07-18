/**
 * Document viewer (REV-004, UI_UX_BLUEPRINT §5.6).
 *
 * Renders the pipeline's page rasters (PRC-004/005) — the server never
 * hands out object keys, so every image resolves through the STO-004
 * signed-URL endpoint with a bounded client-side cache that renews URLs
 * before they expire (and once more on an image error, in case the
 * clock lost the race).
 *
 * Memory stays bounded: only the current page mounts at full size,
 * thumbnails load lazily as they scroll into view, and the signed-URL
 * cache is capped. Controls are buttons with names, the page position
 * is announced via a live region, and the keyboard map is documented on
 * the viewer itself (arrows/PageUp/PageDown pages, +/- zoom, r rotate).
 *
 * Text search runs over the pages' text artifacts when extraction has
 * produced them; until then the search says so honestly instead of
 * silently finding nothing.
 */

import { Badge, Banner, Button, Skeleton, TextField } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  fetchDocumentPages,
  requestArtifactDownload,
  type DocumentPageEntry,
} from "../../api/client";
import {
  describeEvidencePosition,
  fractionToRaster,
  isMeaningfulRect,
  rectPolygon,
  toPercentBox,
} from "./geometry";

const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2];
//: Signed URLs are renewed this long before their stated expiry.
const RENEWAL_MARGIN_MS = 30_000;
//: Bounded page cache: most-recently-used signed URLs only.
const URL_CACHE_LIMIT = 24;

const KEYBOARD_MAP =
  "Keyboard: next page ArrowRight/PageDown, previous page ArrowLeft/PageUp, " +
  "zoom in +, zoom out -, rotate r";

type UrlCache = Map<string, { url: string; expiresAt: number }>;

function useSignedUrl(organizationSlug: string) {
  const cache = useRef<UrlCache>(new Map());
  const inFlight = useRef(new Map<string, Promise<string>>());
  return useCallback(
    async (artifactId: string, options?: { force?: boolean }): Promise<string> => {
      const hit = cache.current.get(artifactId);
      if (!options?.force && hit && hit.expiresAt - Date.now() > RENEWAL_MARGIN_MS) {
        return hit.url;
      }
      const pending = inFlight.current.get(artifactId);
      if (!options?.force && pending !== undefined) return pending;

      const request = requestArtifactDownload(organizationSlug, artifactId).then((signed) => {
        cache.current.delete(artifactId);
        cache.current.set(artifactId, {
          url: signed.url,
          expiresAt: new Date(signed.expires_at).getTime(),
        });
        while (cache.current.size > URL_CACHE_LIMIT) {
          const oldest = cache.current.keys().next().value as string;
          cache.current.delete(oldest);
        }
        return signed.url;
      });
      if (!options?.force) inFlight.current.set(artifactId, request);
      try {
        return await request;
      } finally {
        if (inFlight.current.get(artifactId) === request) {
          inFlight.current.delete(artifactId);
        }
      }
    },
    [organizationSlug],
  );
}

function PageImage({
  artifactId,
  alt,
  resolve,
  style,
  loading,
}: {
  artifactId: string;
  alt: string;
  resolve: (artifactId: string, options?: { force?: boolean }) => Promise<string>;
  style?: React.CSSProperties;
  loading?: "lazy" | "eager";
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const retried = useRef(false);

  useEffect(() => {
    let cancelled = false;
    retried.current = false;
    setUrl(null);
    setFailed(false);
    void resolve(artifactId).then(
      (resolved) => {
        if (!cancelled) setUrl(resolved);
      },
      () => {
        if (!cancelled) setFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [artifactId, resolve]);

  if (failed) {
    return <Badge tone="critical">page unavailable</Badge>;
  }
  if (url === null) {
    return <Skeleton height="6rem" />;
  }
  return (
    <img
      src={url}
      alt={alt}
      style={style}
      loading={loading}
      onError={() => {
        // The signed URL may have expired mid-flight: renew exactly once.
        if (retried.current) {
          setFailed(true);
          return;
        }
        retried.current = true;
        void resolve(artifactId, { force: true }).then(setUrl, () => setFailed(true));
      }}
    />
  );
}

function ThumbnailImage({
  artifactId,
  alt,
  resolve,
  eager,
}: {
  artifactId: string;
  alt: string;
  resolve: (artifactId: string, options?: { force?: boolean }) => Promise<string>;
  eager: boolean;
}) {
  const host = useRef<HTMLSpanElement>(null);
  const [intersected, setIntersected] = useState(false);

  useEffect(() => {
    if (eager || intersected || typeof IntersectionObserver === "undefined") return;
    const node = host.current;
    if (node === null) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setIntersected(true);
          observer.disconnect();
        }
      },
      { rootMargin: "160px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [eager, intersected]);

  return (
    <span ref={host} style={{ display: "block", minHeight: "5.5rem" }}>
      {eager || intersected ? (
        <PageImage
          artifactId={artifactId}
          alt={alt}
          resolve={resolve}
          loading="lazy"
          style={{ width: "100%", display: "block" }}
        />
      ) : (
        <span
          aria-hidden="true"
          style={{ display: "block", height: "5.5rem", background: "var(--soa-surface-sunken)" }}
        />
      )}
    </span>
  );
}

interface SearchHit {
  page_number: number;
  count: number;
}

/** One piece of evidence to highlight (REV-005). Coordinates are the
 * PRC-005 raster pixels; ``polygon: null`` is PAGE-LEVEL evidence — the
 * value is on the page but no exact region was captured, and the overlay
 * says so instead of inventing a box. */
export interface EvidenceHighlight {
  id: string;
  label: string;
  page_number: number;
  polygon: readonly (readonly [number, number])[] | null;
  /** Active = the selected field's evidence; related = candidates/context. */
  kind?: "active" | "related";
}

export function DocumentViewer({
  organizationSlug,
  documentId,
  evidence = [],
  activeEvidenceId = null,
  onEvidenceSelect,
  drawTarget = null,
  onRegionDrawn,
}: {
  organizationSlug: string;
  documentId: string;
  /** Overlays to draw; the field editors (REV-007/008) supply these. */
  evidence?: EvidenceHighlight[];
  /** Field -> source: selecting this id jumps the viewer to its page. */
  activeEvidenceId?: string | null;
  /** Source -> field: fired when the user clicks an overlay. */
  onEvidenceSelect?: (id: string) => void;
  /** When set, the reviewer can draw/adjust the region for this field
   * (a short label for the prompt); null disables editing. */
  drawTarget?: string | null;
  /** A drawn/adjusted region in raster pixels, for the current page. */
  onRegionDrawn?: (region: { pageNumber: number; polygon: number[][] }) => void;
}) {
  const resolve = useSignedUrl(organizationSlug);
  const pagesQuery = useQuery({
    queryKey: ["document-pages", organizationSlug, documentId],
    queryFn: () => fetchDocumentPages(organizationSlug, documentId),
  });
  const [pageNumber, setPageNumber] = useState(1);
  const [zoomIndex, setZoomIndex] = useState(2); // 100%
  const [rotation, setRotation] = useState(0);
  const [searchDraft, setSearchDraft] = useState("");
  const [searchState, setSearchState] = useState<
    | { status: "idle" }
    | { status: "searching" }
    | { status: "done"; term: string; hits: SearchHit[] }
    | { status: "unavailable" }
  >({ status: "idle" });
  const textCache = useRef(new Map<string, string>());

  const pages = pagesQuery.data?.pages ?? [];
  const current: DocumentPageEntry | undefined = pages.find(
    (page) => page.page_number === pageNumber,
  );
  const pageCount = pages.length;
  const zoom = ZOOM_STEPS[zoomIndex];

  // Reviewer-drawn region (REV-005). Editing is armed only with a draw
  // target and an UNROTATED page — a rotated page would need the inverse
  // transform, so we ask the reviewer to reset rotation rather than guess.
  const pageBoxRef = useRef<HTMLDivElement>(null);
  const editingArmed = drawTarget !== null && rotation === 0 && current !== undefined;
  // The drag in progress, in raster pixels: `fixed` is the anchored corner
  // (the drag start for a fresh box, or the opposite corner when resizing).
  const [drag, setDrag] = useState<{ fixed: [number, number]; cursor: [number, number] } | null>(
    null,
  );

  const pointerToRaster = useCallback(
    (clientX: number, clientY: number): [number, number] | null => {
      const box = pageBoxRef.current;
      if (box === null || current === undefined) return null;
      const rect = box.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return null;
      return fractionToRaster(
        (clientX - rect.left) / rect.width,
        (clientY - rect.top) / rect.height,
        current.width_px,
        current.height_px,
      );
    },
    [current],
  );

  const beginDrag = useCallback(
    (event: ReactPointerEvent, fixed: [number, number]) => {
      if (!editingArmed) return;
      const cursor = pointerToRaster(event.clientX, event.clientY);
      if (cursor === null) return;
      event.preventDefault();
      // Capture on the page box so moves route here whether the drag began
      // on empty page (fresh box) or on a corner handle (resize).
      pageBoxRef.current?.setPointerCapture?.(event.pointerId);
      setDrag({ fixed, cursor });
    },
    [editingArmed, pointerToRaster],
  );

  const moveDrag = useCallback(
    (event: ReactPointerEvent) => {
      if (drag === null) return;
      const cursor = pointerToRaster(event.clientX, event.clientY);
      if (cursor !== null) setDrag((prev) => (prev ? { ...prev, cursor } : prev));
    },
    [drag, pointerToRaster],
  );

  const endDrag = useCallback(() => {
    if (drag === null || current === undefined) {
      setDrag(null);
      return;
    }
    const polygon = rectPolygon(drag.fixed, drag.cursor);
    setDrag(null);
    if (isMeaningfulRect(polygon as [number, number][])) {
      onRegionDrawn?.({ pageNumber: current.page_number, polygon });
    }
  }, [drag, current, onRegionDrawn]);

  const goTo = useCallback(
    (target: number) => {
      if (pageCount === 0) return;
      setPageNumber(Math.min(Math.max(target, 1), pageCount));
    },
    [pageCount],
  );

  // Field -> source: when a field's evidence becomes active, show its page.
  useEffect(() => {
    if (activeEvidenceId === null) return;
    const target = evidence.find((entry) => entry.id === activeEvidenceId);
    if (target !== undefined) goTo(target.page_number);
    // Intentionally keyed on the ID: re-render churn in the evidence array
    // must not re-trigger navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeEvidenceId, goTo]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if ((event.target as HTMLElement).tagName === "INPUT") return;
    if (event.key === "ArrowRight" || event.key === "PageDown") {
      event.preventDefault();
      goTo(pageNumber + 1);
    } else if (event.key === "ArrowLeft" || event.key === "PageUp") {
      event.preventDefault();
      goTo(pageNumber - 1);
    } else if (event.key === "+" || event.key === "=") {
      setZoomIndex((index) => Math.min(index + 1, ZOOM_STEPS.length - 1));
    } else if (event.key === "-") {
      setZoomIndex((index) => Math.max(index - 1, 0));
    } else if (event.key.toLowerCase() === "r") {
      // Rotation is a viewer-scoped shortcut. Do not also bubble the same
      // key to Review Studio's global reject/escalate shortcut.
      event.stopPropagation();
      setRotation((value) => (value + 90) % 360);
    }
  };

  const runSearch = async () => {
    const term = searchDraft.trim().toLowerCase();
    if (!term) return;
    const searchable = pages.filter((page) => page.text_artifact_id !== null);
    if (searchable.length === 0) {
      setSearchState({ status: "unavailable" });
      return;
    }
    setSearchState({ status: "searching" });
    const hits: SearchHit[] = [];
    for (const page of searchable) {
      const artifactId = page.text_artifact_id as string;
      let text = textCache.current.get(artifactId);
      if (text === undefined) {
        const url = await resolve(artifactId);
        text = await (await fetch(url)).text();
        textCache.current.set(artifactId, text);
      }
      const count = text.toLowerCase().split(term).length - 1;
      if (count > 0) hits.push({ page_number: page.page_number, count });
    }
    setSearchState({ status: "done", term, hits });
  };

  if (pagesQuery.status === "pending") {
    return <Skeleton height="16rem" />;
  }
  if (pagesQuery.status === "error") {
    return (
      <Banner
        tone="critical"
        title="Couldn’t load the document pages"
        action={
          <Button size="sm" onPress={() => void pagesQuery.refetch()}>
            Try again
          </Button>
        }
      >
        Nothing has been changed.
      </Banner>
    );
  }
  if (pageCount === 0) {
    return (
      <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
        No rendered pages yet — pages appear once processing has run.
      </p>
    );
  }

  return (
    <div
      role="group"
      aria-label="Document viewer"
      aria-description={KEYBOARD_MAP}
      tabIndex={0}
      onKeyDown={onKeyDown}
      style={{ display: "grid", gap: "var(--soa-space-3)" }}
    >
      <div
        style={{
          display: "flex",
          gap: "var(--soa-space-2)",
          alignItems: "end",
          flexWrap: "wrap",
        }}
      >
        <Button
          size="sm"
          variant="subtle"
          aria-label="Previous page"
          isDisabled={pageNumber <= 1}
          onPress={() => goTo(pageNumber - 1)}
        >
          ‹
        </Button>
        <span aria-live="polite" style={{ font: "var(--soa-font-caption)" }}>
          Page {pageNumber} of {pageCount}
        </span>
        <Button
          size="sm"
          variant="subtle"
          aria-label="Next page"
          isDisabled={pageNumber >= pageCount}
          onPress={() => goTo(pageNumber + 1)}
        >
          ›
        </Button>
        <Button
          size="sm"
          variant="subtle"
          aria-label="Zoom out"
          isDisabled={zoomIndex === 0}
          onPress={() => setZoomIndex((index) => Math.max(index - 1, 0))}
        >
          −
        </Button>
        <span style={{ font: "var(--soa-font-caption)" }}>{Math.round(zoom * 100)}%</span>
        <Button
          size="sm"
          variant="subtle"
          aria-label="Zoom in"
          isDisabled={zoomIndex === ZOOM_STEPS.length - 1}
          onPress={() => setZoomIndex((index) => Math.min(index + 1, ZOOM_STEPS.length - 1))}
        >
          +
        </Button>
        <Button
          size="sm"
          variant="subtle"
          aria-label="Rotate page"
          onPress={() => setRotation((value) => (value + 90) % 360)}
        >
          ⟳
        </Button>
        <div style={{ minWidth: "12rem" }}>
          <TextField label="Search text" value={searchDraft} onChange={setSearchDraft} />
        </div>
        <Button size="sm" variant="secondary" onPress={() => void runSearch()}>
          Search
        </Button>
      </div>

      {searchState.status === "unavailable" ? (
        <Banner tone="info" title="Text search isn’t available yet">
          No text has been extracted for this document; search becomes available once text
          extraction runs.
        </Banner>
      ) : null}
      {searchState.status === "done" ? (
        searchState.hits.length === 0 ? (
          <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
            No matches for “{searchState.term}”.
          </p>
        ) : (
          <ul
            aria-label="Search results"
            style={{
              listStyle: "none",
              margin: 0,
              padding: 0,
              display: "flex",
              gap: "0.5rem",
              flexWrap: "wrap",
            }}
          >
            {searchState.hits.map((hit) => (
              <li key={hit.page_number}>
                <Button size="sm" variant="subtle" onPress={() => goTo(hit.page_number)}>
                  Page {hit.page_number} ({hit.count})
                </Button>
              </li>
            ))}
          </ul>
        )
      ) : null}

      <div style={{ display: "flex", gap: "var(--soa-space-4)", alignItems: "flex-start" }}>
        <nav
          aria-label="Pages"
          style={{
            display: "grid",
            gap: "var(--soa-space-2)",
            maxHeight: "24rem",
            overflowY: "auto",
            paddingRight: "var(--soa-space-2)",
          }}
        >
          {pages.map((page) => (
            <button
              key={page.page_number}
              type="button"
              aria-label={`Go to page ${page.page_number}`}
              aria-current={page.page_number === pageNumber ? "page" : undefined}
              onClick={() => goTo(page.page_number)}
              style={{
                border:
                  page.page_number === pageNumber
                    ? "2px solid var(--soa-accent)"
                    : "1px solid var(--soa-border)",
                borderRadius: "var(--soa-radius-control)",
                padding: 2,
                background: "none",
                cursor: "pointer",
                width: "4.5rem",
              }}
            >
              {/* Keep every page keyboard-navigable, but resolve signed URLs
                  only near the current page or viewport. */}
              <ThumbnailImage
                artifactId={page.image_artifact_id}
                alt={`Page ${page.page_number} thumbnail`}
                resolve={resolve}
                eager={Math.abs(page.page_number - pageNumber) <= 1}
              />
              <span style={{ font: "var(--soa-font-caption)" }}>{page.page_number}</span>
            </button>
          ))}
        </nav>

        <div
          style={{
            flex: 1,
            overflow: "auto",
            border: "1px solid var(--soa-border)",
            borderRadius: "var(--soa-radius-panel)",
            background: "var(--soa-surface-sunken, var(--soa-surface))",
            padding: "var(--soa-space-3)",
            maxHeight: "40rem",
          }}
        >
          {current ? (
            // The ROTATION lives on this shared container: the image and
            // every overlay rotate together, and overlays are positioned
            // in percentages of the page box, so zoom needs no recompute
            // either — the coordinate transform cannot drift (REV-005).
            <div
              ref={pageBoxRef}
              data-testid="page-container"
              onPointerDown={
                editingArmed && drag === null
                  ? (event) => {
                      // A press on empty page (not a handle) starts a fresh box.
                      const start = pointerToRaster(event.clientX, event.clientY);
                      if (start !== null) beginDrag(event, start);
                    }
                  : undefined
              }
              onPointerMove={drag !== null ? moveDrag : undefined}
              onPointerUp={drag !== null ? endDrag : undefined}
              style={{
                position: "relative",
                width: `${Math.round(zoom * 100)}%`,
                transform: rotation ? `rotate(${rotation}deg)` : undefined,
                transformOrigin: "center center",
                cursor: editingArmed ? "crosshair" : undefined,
                touchAction: editingArmed ? "none" : undefined,
              }}
            >
              {/* Only the CURRENT page mounts at full size — bounded
                  memory even for very large documents. */}
              <PageImage
                artifactId={current.image_artifact_id}
                alt={`Page ${current.page_number} of ${pageCount}`}
                resolve={resolve}
                loading="eager"
                style={{ width: "100%", maxWidth: "none", display: "block" }}
              />
              {/* Live preview of the box being drawn/resized. */}
              {drag !== null
                ? (() => {
                    const preview = toPercentBox(
                      rectPolygon(drag.fixed, drag.cursor) as [number, number][],
                      current.width_px,
                      current.height_px,
                    );
                    return (
                      <div
                        aria-hidden="true"
                        style={{
                          position: "absolute",
                          left: `${preview.left}%`,
                          top: `${preview.top}%`,
                          width: `${preview.width}%`,
                          height: `${preview.height}%`,
                          border: "2px solid var(--soa-accent)",
                          background: "color-mix(in srgb, var(--soa-accent) 18%, transparent)",
                          pointerEvents: "none",
                        }}
                      />
                    );
                  })()
                : null}
              {evidence
                .filter((entry) => entry.page_number === current.page_number)
                .map((entry) => {
                  const active = entry.id === activeEvidenceId || entry.kind === "active";
                  const description = describeEvidencePosition(
                    entry.polygon,
                    current.width_px,
                    current.height_px,
                  );
                  if (entry.polygon === null) {
                    // Page-level evidence: say so — never invent a box.
                    return (
                      <button
                        key={entry.id}
                        type="button"
                        data-evidence-id={entry.id}
                        aria-label={`Evidence for ${entry.label}: ${description}`}
                        onClick={() => onEvidenceSelect?.(entry.id)}
                        style={{
                          position: "absolute",
                          top: 4,
                          left: 4,
                          border: active
                            ? "2px solid var(--soa-warning)"
                            : "1px dashed var(--soa-warning)",
                          background: "var(--soa-warning)",
                          color: "var(--soa-surface)",
                          borderRadius: "var(--soa-radius-control)",
                          padding: "2px 6px",
                          font: "var(--soa-font-caption)",
                          cursor: "pointer",
                        }}
                      >
                        {entry.label}: somewhere on this page
                      </button>
                    );
                  }
                  const box = toPercentBox(entry.polygon, current.width_px, current.height_px);
                  const xs = entry.polygon.map((p) => p[0]);
                  const ys = entry.polygon.map((p) => p[1]);
                  const [rMinX, rMaxX] = [Math.min(...xs), Math.max(...xs)];
                  const [rMinY, rMaxY] = [Math.min(...ys), Math.max(...ys)];
                  // Each corner handle resizes toward its DIAGONAL opposite,
                  // which stays anchored (in raster px) during the drag.
                  const handles: { cx: number; cy: number; fixed: [number, number] }[] = [
                    { cx: box.left, cy: box.top, fixed: [rMaxX, rMaxY] },
                    { cx: box.left + box.width, cy: box.top, fixed: [rMinX, rMaxY] },
                    { cx: box.left + box.width, cy: box.top + box.height, fixed: [rMinX, rMinY] },
                    { cx: box.left, cy: box.top + box.height, fixed: [rMaxX, rMinY] },
                  ];
                  const showHandles = active && editingArmed && drag === null;
                  return (
                    <div key={entry.id}>
                      <button
                        type="button"
                        data-evidence-id={entry.id}
                        aria-label={`Evidence for ${entry.label}: ${description}`}
                        onClick={() => onEvidenceSelect?.(entry.id)}
                        style={{
                          position: "absolute",
                          left: `${box.left}%`,
                          top: `${box.top}%`,
                          width: `${box.width}%`,
                          height: `${box.height}%`,
                          border: active
                            ? "2px solid var(--soa-accent)"
                            : "2px dashed color-mix(in srgb, var(--soa-accent) 50%, transparent)",
                          background: active
                            ? "color-mix(in srgb, var(--soa-accent) 18%, transparent)"
                            : "color-mix(in srgb, var(--soa-accent) 8%, transparent)",
                          borderRadius: "var(--soa-radius-control)",
                          padding: 0,
                          cursor: "pointer",
                        }}
                      />
                      {showHandles
                        ? handles.map((handle, i) => (
                            <div
                              key={i}
                              role="button"
                              aria-label={`Resize ${entry.label} evidence region`}
                              onPointerDown={(event) => {
                                event.stopPropagation();
                                beginDrag(event, handle.fixed);
                              }}
                              style={{
                                position: "absolute",
                                left: `${handle.cx}%`,
                                top: `${handle.cy}%`,
                                width: 10,
                                height: 10,
                                marginLeft: -5,
                                marginTop: -5,
                                background: "var(--soa-surface)",
                                border: "2px solid var(--soa-accent)",
                                borderRadius: "var(--soa-radius-control)",
                                cursor: "nwse-resize",
                                touchAction: "none",
                              }}
                            />
                          ))
                        : null}
                    </div>
                  );
                })}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
