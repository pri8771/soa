/**
 * Contextual help affordance (GTM-005).
 *
 * A small, unobtrusive "?" that opens a short explanation in a popover —
 * it sits next to the thing it explains and never blocks the work behind
 * it. Content comes from the shared help registry so wording stays
 * consistent everywhere the same concept appears.
 */

import { Button, Dialog, DialogTrigger } from "@soa/design-system";

import { HELP_TOPICS, type HelpTopicKey } from "./helpContent";

export function HelpTip({ topic }: { topic: HelpTopicKey }) {
  const content = HELP_TOPICS[topic];
  return (
    <DialogTrigger>
      <Button variant="subtle" size="sm" aria-label={`Help: ${content.title}`}>
        ?
      </Button>
      <Dialog title={content.title}>
        <p style={{ margin: 0, maxWidth: "24rem", fontSize: "0.9rem", lineHeight: 1.5 }}>
          {content.body}
        </p>
      </Dialog>
    </DialogTrigger>
  );
}
