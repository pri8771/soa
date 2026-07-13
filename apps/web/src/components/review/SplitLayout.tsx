/**
 * Resizable split layout for the Review Studio (REV-015).
 *
 * Wide screens get the viewer and the editor side by side with a
 * keyboard-accessible separator between them (drag with the pointer, or
 * focus it and use the arrow keys); the chosen split is persisted per
 * browser. Below the compact breakpoint the panels STACK — nothing is
 * hidden or trapped behind the resize affordance — in the defined
 * read/approve order: document first, then the fields, then the
 * completion actions that follow this component on the page.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";

//: Side-by-side needs roughly this much room; below it (tablets,
//: split-screen desktops) the panels stack. 1280x720 stays side by side.
const COMPACT_QUERY = "(max-width: 1023px)";
const MIN_RATIO = 25;
const MAX_RATIO = 75;
const KEY_STEP = 5;

function readStoredRatio(storageKey: string): number {
  try {
    const raw = window.localStorage.getItem(storageKey);
    const parsed = raw === null ? NaN : Number(raw);
    if (Number.isFinite(parsed)) return Math.min(MAX_RATIO, Math.max(MIN_RATIO, parsed));
  } catch {
    // Storage may be unavailable (private mode); fall through.
  }
  return 60;
}

export function useCompactLayout(): boolean {
  const [compact, setCompact] = useState<boolean>(() =>
    typeof window.matchMedia === "function" ? window.matchMedia(COMPACT_QUERY).matches : false,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(COMPACT_QUERY);
    const onChange = (event: MediaQueryListEvent) => setCompact(event.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return compact;
}

export interface SplitLayoutProps {
  /** localStorage key for the persisted split preference. */
  storageKey: string;
  left: ReactNode;
  right: ReactNode;
  leftLabel: string;
  rightLabel: string;
}

export function SplitLayout(props: SplitLayoutProps) {
  const compact = useCompactLayout();
  const [ratio, setRatio] = useState<number>(() => readStoredRatio(props.storageKey));
  const containerRef = useRef<HTMLDivElement | null>(null);
  const dragging = useRef(false);

  const apply = (next: number) => {
    const clamped = Math.min(MAX_RATIO, Math.max(MIN_RATIO, Math.round(next)));
    setRatio(clamped);
    try {
      window.localStorage.setItem(props.storageKey, String(clamped));
    } catch {
      // Preference persistence is best effort.
    }
  };

  if (compact) {
    // Stacked read/approve path: everything remains reachable; only the
    // resize affordance disappears (it has no meaning when stacked).
    return (
      <div style={{ display: "grid", gap: "var(--soa-space-4)" }} data-testid="split-stacked">
        <div aria-label={props.leftLabel}>{props.left}</div>
        <div aria-label={props.rightLabel}>{props.right}</div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      data-testid="split-side-by-side"
      style={{
        display: "grid",
        gridTemplateColumns: `minmax(0, ${ratio}fr) auto minmax(20rem, ${100 - ratio}fr)`,
        gap: "var(--soa-space-2)",
        alignItems: "start",
      }}
      onPointerMove={(event) => {
        if (!dragging.current || !containerRef.current) return;
        const bounds = containerRef.current.getBoundingClientRect();
        if (bounds.width > 0) {
          apply(((event.clientX - bounds.left) / bounds.width) * 100);
        }
      }}
      onPointerUp={() => {
        dragging.current = false;
      }}
    >
      <div aria-label={props.leftLabel} style={{ minWidth: 0 }}>
        {props.left}
      </div>
      <div
        role="separator"
        tabIndex={0}
        aria-orientation="vertical"
        aria-label="Resize the document and fields panels"
        aria-valuemin={MIN_RATIO}
        aria-valuemax={MAX_RATIO}
        aria-valuenow={ratio}
        onPointerDown={(event) => {
          dragging.current = true;
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") {
            event.preventDefault();
            apply(ratio - KEY_STEP);
          } else if (event.key === "ArrowRight") {
            event.preventDefault();
            apply(ratio + KEY_STEP);
          }
        }}
        style={{
          cursor: "col-resize",
          width: "0.5rem",
          alignSelf: "stretch",
          borderInline: "1px solid var(--soa-border)",
          borderRadius: "2px",
        }}
      />
      <div aria-label={props.rightLabel} style={{ minWidth: 0 }}>
        {props.right}
      </div>
    </div>
  );
}
