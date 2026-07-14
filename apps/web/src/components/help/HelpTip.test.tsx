import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderApp } from "../../test/render";

describe("In-app help (GTM-005)", () => {
  it("opens a contextual explanation from the upload screen without leaving it", async () => {
    const user = userEvent.setup();
    await renderApp("/app/northstar/documents/upload");

    // The help affordance sits next to the work, labelled for its topic.
    const help = await screen.findByRole("button", { name: "Help: Uploading documents" });
    await user.click(help);

    // The concise explanation appears in a popover titled for the topic —
    // shown in place, without navigating away from the upload screen.
    await waitFor(() => {
      expect(screen.getByText(/Drop in a PDF or image of a purchase order/)).toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { name: "Uploading documents" })).toBeInTheDocument();
  });
});
