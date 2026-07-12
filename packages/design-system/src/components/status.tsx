import type { ReactNode } from "react";
import {
  ProgressBar as AriaProgressBar,
  type ProgressBarProps as AriaProgressBarProps,
} from "react-aria-components";

export type StatusTone = "neutral" | "accent" | "success" | "warning" | "critical" | "info";

export interface BadgeProps {
  tone?: StatusTone;
  children: ReactNode;
  className?: string;
}

/** Status chip. Meaning is conveyed by text/icon, never color alone. */
export function Badge({ tone = "neutral", children, className }: BadgeProps) {
  return (
    <span className={`soa-badge ${className ?? ""}`} data-tone={tone}>
      {children}
    </span>
  );
}

export interface BannerProps {
  tone?: StatusTone;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}

/** Persistent inline banner for ongoing conditions (not transient toasts). */
export function Banner({ tone = "info", title, children, action }: BannerProps) {
  return (
    <div className="soa-banner" data-tone={tone} role={tone === "critical" ? "alert" : "status"}>
      <div className="soa-banner-body">
        <p className="soa-banner-title">{title}</p>
        {children ? <div className="soa-banner-content">{children}</div> : null}
      </div>
      {action ? <div className="soa-banner-action">{action}</div> : null}
    </div>
  );
}

export interface ProgressBarProps extends AriaProgressBarProps {
  label: string;
}

export function ProgressBar({ label, ...props }: ProgressBarProps) {
  return (
    <AriaProgressBar
      {...props}
      aria-label={label}
      className={`soa-progress ${props.className ?? ""}`}
    >
      {({ percentage, isIndeterminate }) => (
        <span className="soa-progress-track" data-indeterminate={isIndeterminate}>
          <span
            className="soa-progress-fill"
            style={isIndeterminate ? undefined : { width: `${percentage ?? 0}%` }}
          />
        </span>
      )}
    </AriaProgressBar>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span className="soa-spinner" role="progressbar" aria-label={label}>
      <span aria-hidden="true" className="soa-spinner-ring" />
    </span>
  );
}

export interface SkeletonProps {
  /** Matches the final layout it stands in for (UI_UX_BLUEPRINT §9). */
  width?: string;
  height?: string;
  className?: string;
}

export function Skeleton({ width = "100%", height = "1rem", className }: SkeletonProps) {
  return (
    <span
      aria-hidden="true"
      className={`soa-skeleton ${className ?? ""}`}
      style={{ width, height }}
    />
  );
}
