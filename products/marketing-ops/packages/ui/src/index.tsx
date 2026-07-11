import type { HTMLAttributes, ReactNode } from "react";

export function ProductMark({ compact = false }: { readonly compact?: boolean }) {
  return (
    <span aria-label="Marketing Ops" className="mo-product-mark">
      <span aria-hidden="true" className="mo-product-mark__glyph">
        M
      </span>
      {compact ? null : <span className="mo-product-mark__name">Marketing Ops</span>}
    </span>
  );
}

export function StatusPill({
  children,
  tone = "neutral",
}: {
  readonly children: ReactNode;
  readonly tone?: "neutral" | "positive" | "warning" | "danger" | "accent";
}) {
  return <span className={`mo-status-pill mo-status-pill--${tone}`}>{children}</span>;
}

export function MetricCard({
  change,
  detail,
  label,
  value,
}: {
  readonly change?: string;
  readonly detail: string;
  readonly label: string;
  readonly value: string;
}) {
  return (
    <article className="mo-metric-card">
      <div className="mo-metric-card__header">
        <span>{label}</span>
        {change ? <span className="mo-metric-card__change">{change}</span> : null}
      </div>
      <strong>{value}</strong>
      <p>{detail}</p>
    </article>
  );
}

export function Surface({
  children,
  className = "",
  ...props
}: HTMLAttributes<HTMLElement> & { readonly children: ReactNode }) {
  return (
    <section className={`mo-surface ${className}`.trim()} {...props}>
      {children}
    </section>
  );
}
