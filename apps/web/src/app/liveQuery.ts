/**
 * Shared live-update policy for read-only queue and dashboard queries.
 *
 * Apply ONLY to queries whose screens are pure read models (queues,
 * dashboards, status lists) — never to screens holding draft or form
 * state, where a background refetch could pull the data out from under
 * an edit in progress.
 */

/** Poll cadence for operational queues (documents, review, jobs). */
export const QUEUE_POLL_MS = 15_000;

/** Poll cadence for aggregate dashboards (overview, costs, quality). */
export const DASHBOARD_POLL_MS = 60_000;

export function liveQueryOptions(refetchInterval: number) {
  return { refetchInterval, refetchOnWindowFocus: true as const };
}
