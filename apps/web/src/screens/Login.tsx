import { Banner, Button, Spinner } from "@soa/design-system";
import { useLocation, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { useAuth } from "../auth/AuthContext";
import { sanitizeReturnTo } from "../auth/oidc";
import { consumePostLoginDestination } from "../auth/runtime";

interface LoginSearch {
  returnTo?: string;
  reason?: "expired" | "configuration";
  code?: string;
  state?: string;
  error?: string;
  error_description?: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Sign-in could not be completed. Try again.";
}

export function Login() {
  const auth = useAuth();
  const search = useSearch({ strict: false }) as LoginSearch;
  const location = useLocation();
  const navigate = useNavigate();
  const callbackStarted = useRef(false);
  const [busy, setBusy] = useState(false);
  const [callbackError, setCallbackError] = useState<string | null>(null);
  const returnTo = sanitizeReturnTo(search.returnTo);
  const isCallback = Boolean(search.code || search.state || search.error);

  useEffect(() => {
    if (!isCallback || callbackStarted.current) return;
    callbackStarted.current = true;
    setBusy(true);
    const callbackUrl = new URL(globalThis.location.href);
    callbackUrl.search = location.searchStr;
    void auth
      .completeLogin(callbackUrl.toString())
      .then((destination) => navigate({ to: sanitizeReturnTo(destination) as "/", replace: true }))
      .catch((error: unknown) => {
        setCallbackError(errorMessage(error));
        setBusy(false);
      });
  }, [auth, isCallback, location.searchStr, navigate]);

  useEffect(() => {
    if (!isCallback && auth.isAuthenticated) {
      const destination = sanitizeReturnTo(
        search.returnTo ?? consumePostLoginDestination() ?? returnTo,
      );
      void navigate({ to: destination as "/", replace: true });
    }
  }, [auth.isAuthenticated, isCallback, navigate, returnTo, search.returnTo]);

  const startLogin = async () => {
    setBusy(true);
    setCallbackError(null);
    try {
      await auth.beginLogin(returnTo);
    } catch (error) {
      setCallbackError(errorMessage(error));
      setBusy(false);
    }
  };

  return (
    <main
      style={{
        maxWidth: "28rem",
        margin: "10vh auto",
        padding: "var(--soa-space-6)",
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        background: "var(--soa-surface)",
        display: "grid",
        gap: "var(--soa-space-5)",
      }}
    >
      <div>
        <p style={{ margin: 0, color: "var(--soa-text-secondary)" }}>SOA</p>
        <h1 style={{ margin: "var(--soa-space-2) 0 0", font: "var(--soa-font-heading-xl)" }}>
          Sign in
        </h1>
        <p style={{ color: "var(--soa-text-secondary)" }}>
          Use your organization identity. Access is determined by your active SOA memberships.
        </p>
      </div>

      {search.reason === "expired" || auth.status === "expired" ? (
        <Banner tone="warning" title="Your session expired">
          Sign in again to continue. The page you were using will reopen afterward.
        </Banner>
      ) : null}

      {search.reason === "configuration" || auth.status === "configuration-error" ? (
        <Banner tone="critical" title="Single sign-on is not configured">
          This deployment is missing required browser identity configuration. Contact an
          administrator.
        </Banner>
      ) : null}

      {callbackError ? (
        <Banner tone="critical" title="Sign-in failed">
          {callbackError}
        </Banner>
      ) : null}

      {auth.error && !callbackError ? (
        <Banner tone="critical" title="Sign-in unavailable">
          {auth.error}
        </Banner>
      ) : null}

      {busy || (isCallback && !callbackError) ? (
        <div
          role="status"
          style={{ display: "flex", alignItems: "center", gap: "var(--soa-space-3)" }}
        >
          <Spinner label="Completing sign-in" />
          <span>Completing secure sign-in…</span>
        </div>
      ) : (
        <Button variant="primary" onPress={() => void startLogin()}>
          Continue with single sign-on
        </Button>
      )}

      <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
        Multi-factor authentication and password policy are managed by your identity provider.
      </p>
    </main>
  );
}
