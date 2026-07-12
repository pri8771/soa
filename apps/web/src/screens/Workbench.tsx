/**
 * Component workbench (DSN-003) — every primitive in its required states.
 *
 * Serves the role of Storybook for this codebase: reviewers browse states
 * here, and visual-regression CI (DSN-008) screenshots each
 * data-workbench-section. Sections must stay deterministic: no live time,
 * no animation-dependent end states, realistic long-text samples included.
 */

import {
  Badge,
  Banner,
  Button,
  Checkbox,
  ComboBox,
  Dialog,
  DialogTrigger,
  Menu,
  MenuItem,
  MenuTrigger,
  ProgressBar,
  Radio,
  RadioGroup,
  Select,
  Skeleton,
  Spinner,
  Switch,
  Tab,
  TabList,
  TabPanel,
  Tabs,
  TextField,
  ToastProvider,
  useToast,
} from "@soa/design-system";
import { useState } from "react";

const LONG_LABEL =
  "Extraordinarily long customer name that exercises truncation and wrapping: " +
  "Internationale Groothandelsmaatschappij voor Levensmiddelen en Aanverwante Producten B.V.";

const STREAMS = [
  { id: "uk", label: "United Kingdom" },
  { id: "es", label: "Spain" },
  { id: "de", label: "Germany (Deutschland) — long option label for expansion testing" },
];

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section
      data-workbench-section={id}
      style={{ padding: "var(--soa-space-6)", borderBottom: "1px solid var(--soa-border)" }}
    >
      <h2 style={{ font: "var(--soa-font-heading-md)", margin: "0 0 var(--soa-space-4)" }}>
        {title}
      </h2>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "var(--soa-space-4)",
          alignItems: "center",
        }}
      >
        {children}
      </div>
    </section>
  );
}

function ToastDemo() {
  const { publish } = useToast();
  return (
    <Button variant="secondary" onPress={() => publish("Order approved", { tone: "success" })}>
      Publish toast
    </Button>
  );
}

export function Workbench() {
  const [theme, setTheme] = useState<"light" | "dark">("light");

  return (
    <ToastProvider>
      <main
        data-theme-root
        style={{
          background: "var(--soa-canvas)",
          color: "var(--soa-text-primary)",
          minHeight: "100vh",
        }}
      >
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "var(--soa-space-4) var(--soa-space-6)",
            borderBottom: "1px solid var(--soa-border)",
          }}
        >
          <h1 style={{ font: "var(--soa-font-heading-xl)", margin: 0 }}>Component workbench</h1>
          <Switch
            isSelected={theme === "dark"}
            onChange={(selected) => {
              const next = selected ? "dark" : "light";
              setTheme(next);
              document.documentElement.setAttribute("data-theme", next);
            }}
          >
            Dark theme
          </Switch>
        </header>

        <Section id="buttons" title="Buttons — variants, sizes, disabled">
          <Button variant="primary">Primary</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="subtle">Subtle</Button>
          <Button variant="destructive">Reject document</Button>
          <Button variant="primary" size="sm">
            Small primary
          </Button>
          <Button variant="primary" isDisabled>
            Disabled
          </Button>
          <Button variant="secondary">{LONG_LABEL.slice(0, 60)}</Button>
        </Section>

        <Section id="fields" title="Fields — default, description, error, multiline, disabled">
          <TextField label="PO number" placeholder="PO-90001" />
          <TextField label="Customer" description="As printed on the order header" />
          <TextField label="Quantity" isInvalid errorMessage="Quantity must be positive" />
          <TextField label="Notes" multiline placeholder="Internal comments" />
          <TextField label="Locked field" isDisabled value="read only" />
        </Section>

        <Section id="selection" title="Select and ComboBox">
          <Select label="Stream" items={STREAMS} defaultSelectedKey="uk" />
          <ComboBox label="Material search" items={STREAMS} />
        </Section>

        <Section id="toggles" title="Checkbox, radio, switch — states">
          <Checkbox>Include archived</Checkbox>
          <Checkbox isSelected>Selected</Checkbox>
          <Checkbox isIndeterminate>Partial selection</Checkbox>
          <Checkbox isDisabled>Disabled</Checkbox>
          <RadioGroup label="Density" defaultValue="dense">
            <Radio value="dense">Dense</Radio>
            <Radio value="comfortable">Comfortable</Radio>
          </RadioGroup>
          <Switch defaultSelected>Auto-assign</Switch>
        </Section>

        <Section id="overlays" title="Dialog, menu (triggers)">
          <DialogTrigger>
            <Button variant="secondary">Open dialog</Button>
            <Dialog title="Approval summary">
              {({ close }) => (
                <>
                  <p style={{ font: "var(--soa-font-body-md)" }}>
                    2 warnings remain. Export destination: ERP webhook.
                  </p>
                  <Button variant="primary" onPress={close}>
                    Approve order
                  </Button>
                </>
              )}
            </Dialog>
          </DialogTrigger>
          <MenuTrigger>
            <Button variant="secondary">Row actions</Button>
            <Menu>
              <MenuItem id="assign">Assign reviewer</MenuItem>
              <MenuItem id="reprocess">Reprocess</MenuItem>
              <MenuItem id="archive">Archive</MenuItem>
            </Menu>
          </MenuTrigger>
          <ToastDemo />
        </Section>

        <Section id="tabs" title="Tabs">
          <Tabs defaultSelectedKey="summary" style={{ width: "100%" }}>
            <TabList aria-label="Document detail">
              <Tab id="summary">Summary</Tab>
              <Tab id="validation">Validation</Tab>
              <Tab id="audit">Audit timeline</Tab>
            </TabList>
            <TabPanel id="summary">Header fields and key issues.</TabPanel>
            <TabPanel id="validation">3 failed rules, 1 warning.</TabPanel>
            <TabPanel id="audit">Immutable event history.</TabPanel>
          </Tabs>
        </Section>

        <Section id="status" title="Badges, banners, progress, skeleton">
          <Badge tone="neutral">Draft</Badge>
          <Badge tone="accent">Processing</Badge>
          <Badge tone="success">Approved</Badge>
          <Badge tone="warning">Needs review</Badge>
          <Badge tone="critical">Export failed</Badge>
          <Badge tone="info">Queued</Badge>
          <div style={{ width: "100%", display: "grid", gap: "var(--soa-space-3)" }}>
            <Banner
              tone="critical"
              title="Delivery failing"
              action={<Button size="sm">Retry</Button>}
            >
              The destination rejected customer code GB-1042. Your approved order has not been
              changed. Reference: EXP-7H2K.
            </Banner>
            <Banner tone="info" title="Maintenance window Sunday 02:00–03:00 UTC" />
          </div>
          <div style={{ width: 240 }}>
            <ProgressBar label="Processing document" value={62} />
          </div>
          <Spinner label="Loading queue" />
          <Skeleton width="220px" height="1rem" />
          <Skeleton width="140px" height="2.5rem" />
        </Section>

        <Section id="long-text" title="Localization expansion / long content">
          <p style={{ font: "var(--soa-font-body-md)", maxWidth: 420 }}>{LONG_LABEL}</p>
          <Badge tone="warning">{LONG_LABEL.slice(0, 40)}…</Badge>
        </Section>
      </main>
    </ToastProvider>
  );
}
