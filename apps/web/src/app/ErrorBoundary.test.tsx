import { render, screen } from "@testing-library/react";

import { ErrorBoundary } from "./ErrorBoundary";

function Bomb(): never {
  throw new Error("render failure");
}

describe("ErrorBoundary", () => {
  it("renders children when nothing fails", () => {
    render(
      <ErrorBoundary>
        <p>content</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("content")).toBeInTheDocument();
  });

  it("shows the designed fatal-error state with a next action", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload the application" })).toBeInTheDocument();
    consoleError.mockRestore();
  });
});
