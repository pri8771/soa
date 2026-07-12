import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

/**
 * Last-resort boundary for unexpected render failures. The designed state
 * explains what happened and offers a next action instead of a blank page.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Structured client telemetry arrives with FND-009; keep the console
    // record so local debugging is not silent.
    console.error("Unhandled application error", error, info.componentStack);
  }

  handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.hasError) {
      return this.props.children;
    }
    return (
      <main role="alert" style={{ maxWidth: "36rem", margin: "4rem auto", padding: "0 1.5rem" }}>
        <h1>Something went wrong</h1>
        <p>
          The application hit an unexpected error. Your submitted work is saved on the server and
          has not been changed.
        </p>
        <p>
          <button type="button" onClick={this.handleReload}>
            Reload the application
          </button>
        </p>
      </main>
    );
  }
}
