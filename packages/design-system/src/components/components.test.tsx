import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { axe } from "../test/axe";

import { Button } from "./Button";
import { TextField } from "./fields";
import { Dialog, DialogTrigger, Menu, MenuItem, MenuTrigger } from "./overlays";
import { Select } from "./selection";
import { Badge, Banner, ProgressBar, Skeleton } from "./status";
import { Tab, TabList, TabPanel, Tabs } from "./Tabs";
import { ToastProvider, useToast } from "./toast";
import { Checkbox, Radio, RadioGroup, Switch } from "./toggles";

describe("Button", () => {
  it("renders a real <button> element (no div-button pattern)", () => {
    render(<Button variant="primary">Approve</Button>);
    const button = screen.getByRole("button", { name: "Approve" });
    expect(button.tagName).toBe("BUTTON");
  });

  it("activates via keyboard", async () => {
    const user = userEvent.setup();
    const onPress = vi.fn();
    render(<Button onPress={onPress}>Go</Button>);
    await user.tab();
    expect(screen.getByRole("button")).toHaveFocus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onPress).toHaveBeenCalledTimes(2);
  });

  it("has no axe violations", async () => {
    const { container } = render(<Button>Accessible</Button>);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("TextField", () => {
  it("associates label, description, and input for AT", async () => {
    render(<TextField label="PO number" description="From the document header" />);
    const input = screen.getByRole("textbox", { name: "PO number" });
    expect(input).toHaveAccessibleDescription("From the document header");
  });

  it("has no axe violations", async () => {
    const { container } = render(<TextField label="Customer" />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("Dialog", () => {
  function DialogHarness() {
    return (
      <DialogTrigger>
        <Button>Open dialog</Button>
        <Dialog title="Confirm approval">
          {({ close }) => (
            <>
              <p>3 warnings remain.</p>
              <Button onPress={close}>Close</Button>
            </>
          )}
        </Dialog>
      </DialogTrigger>
    );
  }

  it("traps focus, closes on Escape, and restores focus to the trigger", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    const trigger = screen.getByRole("button", { name: "Open dialog" });
    await user.tab();
    expect(trigger).toHaveFocus();
    await user.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog", { name: "Confirm approval" });
    expect(dialog).toBeInTheDocument();
    // Focus is trapped inside the dialog.
    expect(dialog.contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("alert variant exposes alertdialog role", async () => {
    const user = userEvent.setup();
    render(
      <DialogTrigger>
        <Button>Delete</Button>
        <Dialog title="Really delete?" alert>
          <p>This cannot be undone.</p>
        </Dialog>
      </DialogTrigger>,
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
  });
});

describe("Menu", () => {
  it("opens with keyboard and moves focus through items", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(
      <MenuTrigger>
        <Button>Actions</Button>
        <Menu onAction={onAction}>
          <MenuItem id="reassign">Reassign</MenuItem>
          <MenuItem id="escalate">Escalate</MenuItem>
        </Menu>
      </MenuTrigger>,
    );
    await user.tab();
    await user.keyboard("{Enter}");
    const items = await screen.findAllByRole("menuitem");
    expect(items).toHaveLength(2);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onAction).toHaveBeenCalled();
  });
});

describe("Select", () => {
  it("exposes listbox semantics and selects with keyboard", async () => {
    const user = userEvent.setup();
    render(
      <Select
        label="Stream"
        items={[
          { id: "uk", label: "United Kingdom" },
          { id: "es", label: "Spain" },
        ]}
      />,
    );
    await user.tab();
    await user.keyboard("{Enter}");
    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["United Kingdom", "Spain"]);
  });
});

describe("Toggles", () => {
  it("checkbox and switch use real checkbox/switch semantics", async () => {
    const user = userEvent.setup();
    render(
      <>
        <Checkbox>Include archived</Checkbox>
        <Switch>Auto-approve</Switch>
      </>,
    );
    const checkbox = screen.getByRole("checkbox", { name: "Include archived" });
    const toggle = screen.getByRole("switch", { name: "Auto-approve" });
    await user.click(checkbox);
    expect(checkbox).toBeChecked();
    await user.click(toggle);
    expect(toggle).toBeChecked();
  });

  it("radio group supports arrow-key movement", async () => {
    const user = userEvent.setup();
    render(
      <RadioGroup label="Density">
        <Radio value="dense">Dense</Radio>
        <Radio value="comfortable">Comfortable</Radio>
      </RadioGroup>,
    );
    await user.tab();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("radio", { name: "Comfortable" })).toBeChecked();
  });
});

describe("Tabs", () => {
  it("navigates panels with arrow keys", async () => {
    const user = userEvent.setup();
    render(
      <Tabs>
        <TabList aria-label="Document detail">
          <Tab id="summary">Summary</Tab>
          <Tab id="audit">Audit</Tab>
        </TabList>
        <TabPanel id="summary">Summary content</TabPanel>
        <TabPanel id="audit">Audit content</TabPanel>
      </Tabs>,
    );
    expect(screen.getByText("Summary content")).toBeInTheDocument();
    await user.tab();
    await user.keyboard("{ArrowRight}");
    expect(await screen.findByText("Audit content")).toBeInTheDocument();
  });
});

describe("Status components", () => {
  it("badge conveys meaning through text, not color alone", () => {
    render(<Badge tone="critical">Export failed</Badge>);
    expect(screen.getByText("Export failed")).toBeInTheDocument();
  });

  it("critical banner is an alert; others are status", () => {
    render(
      <>
        <Banner tone="critical" title="Delivery failing" />
        <Banner tone="info" title="Maintenance window" />
      </>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Delivery failing");
    expect(screen.getByRole("status")).toHaveTextContent("Maintenance window");
  });

  it("progress bar exposes value semantics", () => {
    render(<ProgressBar label="Processing" value={40} />);
    expect(screen.getByRole("progressbar", { name: "Processing" })).toHaveAttribute(
      "aria-valuenow",
      "40",
    );
  });

  it("skeleton is hidden from assistive technology", () => {
    const { container } = render(<Skeleton width="120px" />);
    expect(container.querySelector(".soa-skeleton")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("Toast", () => {
  function ToastHarness() {
    const { publish } = useToast();
    return <Button onPress={() => publish("Order approved", { tone: "success" })}>Notify</Button>;
  }

  it("publishes into a polite live region and dismisses", async () => {
    const user = userEvent.setup();
    render(
      <ToastProvider>
        <ToastHarness />
      </ToastProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Notify" }));
    expect(await screen.findByText("Order approved")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Notifications" }).querySelector("[aria-live='polite']"),
    ).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Dismiss notification" }));
    await waitFor(() => expect(screen.queryByText("Order approved")).not.toBeInTheDocument());
  });
});

describe("Accessibility sweep", () => {
  function StateHarness() {
    const [checked, setChecked] = useState(false);
    return (
      <main>
        <h1>Component sweep</h1>
        <Button variant="primary">Primary</Button>
        <TextField label="Name" description="Full legal name" />
        <Checkbox isSelected={checked} onChange={setChecked}>
          Agree
        </Checkbox>
        <RadioGroup label="Mode">
          <Radio value="a">A</Radio>
          <Radio value="b">B</Radio>
        </RadioGroup>
        <Switch>Enabled</Switch>
        <Badge tone="success">Done</Badge>
        <Banner tone="info" title="Heads up" />
        <ProgressBar label="Upload" value={10} />
        <Tabs>
          <TabList aria-label="Sections">
            <Tab id="one">One</Tab>
            <Tab id="two">Two</Tab>
          </TabList>
          <TabPanel id="one">First</TabPanel>
          <TabPanel id="two">Second</TabPanel>
        </Tabs>
      </main>
    );
  }

  it("full harness has no axe violations", async () => {
    const { container } = render(<StateHarness />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
