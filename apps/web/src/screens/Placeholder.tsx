import { AppShell } from "../shell/AppShell";

/** Stand-in screen for areas whose real UI arrives with a later epic. */
export function makePlaceholderScreen(title: string, owningEpic: string) {
  return function PlaceholderScreen() {
    return (
      <AppShell title={title} breadcrumbs={[{ label: title }]}>
        <div
          style={{
            border: "1px dashed var(--soa-border-strong)",
            borderRadius: "var(--soa-radius-panel)",
            padding: "var(--soa-space-8)",
            maxWidth: "36rem",
          }}
        >
          <h2 style={{ font: "var(--soa-font-heading-md)", marginTop: 0 }}>
            {title} is not built yet
          </h2>
          <p style={{ font: "var(--soa-font-body-md)", color: "var(--soa-text-secondary)" }}>
            This area arrives with the {owningEpic} epic of the build backlog. The navigation entry
            exists now so the shell, permissions, and routing are real from the start.
          </p>
        </div>
      </AppShell>
    );
  };
}
