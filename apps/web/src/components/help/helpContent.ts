/**
 * In-app help content (GTM-005).
 *
 * Concise, contextual explanations for the moments people get stuck:
 * upload, review, confidence, publish, replay, and errors. Each entry is
 * a few sentences — enough to unblock without pulling the reader out of
 * their work — and names the canonical concept so it stays consistent
 * with the product docs. Copy lives here (not inline in screens) so it is
 * reviewed and translated in one place.
 */

export interface HelpTopic {
  title: string;
  body: string;
}

export const HELP_TOPICS = {
  upload: {
    title: "Uploading documents",
    body:
      "Drop in a PDF or image of a purchase order and pick the stream it belongs to. " +
      "We validate the file, scan it, and start processing automatically — you don't " +
      "trigger extraction yourself. Duplicates of a document you've already sent are flagged, " +
      "not silently reprocessed.",
  },
  review: {
    title: "Reviewing extractions",
    body:
      "Review is where a person confirms or corrects what the model read. Fields the system " +
      "is confident about are pre-filled; low-confidence or rule-flagged fields are highlighted " +
      "for you. Every value shows its evidence on the document, and your corrections are saved " +
      "as you go.",
  },
  confidence: {
    title: "What confidence means",
    body:
      "Confidence is the model's own estimate that a value is right — it is one signal, never " +
      "the decision. The confidence policy routes low-confidence fields to human review and lets " +
      "high-confidence, rule-passing documents through. A number alone never auto-approves an order.",
  },
  publish: {
    title: "Publishing configuration",
    body:
      "Schemas, rules, and mappings are versioned. Draft changes never affect live processing " +
      "until you publish them, and a published version is immutable — so a running document " +
      "always resolves against a fixed, auditable configuration. To change live behavior, publish " +
      "a new version; to undo, roll back to a prior one.",
  },
  replay: {
    title: "Reprocessing and replay",
    body:
      "Reprocessing runs a document through the pipeline again — for example after you fix a bad " +
      "mapping or publish a corrected rule. It creates a NEW run tied to the selected configuration " +
      "versions; the original run and its evidence are kept, so you can compare. Approved orders can " +
      "be re-exported deterministically.",
  },
  errors: {
    title: "When something fails",
    body:
      "Failures are classified as retryable (transient — the platform retries with backoff) or " +
      "terminal (needs a fix — e.g. an invalid mapping or a rejected credential). Terminal failures " +
      "surface a safe reason and route to review or the Jobs queue; they never loop forever and never " +
      "leak document contents into the message.",
  },
} satisfies Record<string, HelpTopic>;

export type HelpTopicKey = keyof typeof HELP_TOPICS;
