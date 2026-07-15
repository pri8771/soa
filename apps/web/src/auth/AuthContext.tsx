import { Banner, Spinner } from "@soa/design-system";
import { useLocation, useNavigate } from "@tanstack/react-router";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import {
  beginLogin as beginOidcLogin,
  bootstrapAuth,
  completeLogin as completeOidcLogin,
  expireAuthSession,
  forceRefreshBearerToken,
  getAuthSnapshot,
  logout as logoutOidc,
  subscribeAuth,
  type AuthSnapshot,
  type AuthStatus,
} from "./runtime";

type TestAuthStatus = "test" | "anonymous" | "expired" | "configuration-error";

interface AuthContextValue extends AuthSnapshot {
  isAuthenticated: boolean;
  beginLogin(returnTo?: string): Promise<void>;
  completeLogin(callbackUrl: string): Promise<string>;
  logout(): Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function authenticated(status: AuthStatus): boolean {
  return status === "authenticated" || status === "development" || status === "test";
}

function testSnapshot(status: TestAuthStatus): AuthSnapshot {
  return {
    status,
    expiresAt: null,
    canRefresh: false,
    error:
      status === "configuration-error"
        ? "Single sign-on is not configured for this deployment."
        : null,
  };
}

export function AuthProvider({
  children,
  testStatus,
}: {
  children: ReactNode;
  /** Deterministic route-state override used only by the browser test harness. */
  testStatus?: TestAuthStatus;
}) {
  const runtimeSnapshot = useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthSnapshot);
  const snapshot = useMemo(
    () => (testStatus ? testSnapshot(testStatus) : runtimeSnapshot),
    [runtimeSnapshot, testStatus],
  );

  useEffect(() => {
    if (!testStatus) void bootstrapAuth();
  }, [testStatus]);

  useEffect(() => {
    if (testStatus || snapshot.status !== "authenticated" || !snapshot.expiresAt) return;
    let expiryTimer: ReturnType<typeof setTimeout> | undefined;
    const refreshAt = snapshot.canRefresh ? snapshot.expiresAt - 60_000 : snapshot.expiresAt;
    const refreshTimer = setTimeout(
      () => {
        if (!snapshot.canRefresh) {
          expireAuthSession();
          return;
        }
        void forceRefreshBearerToken().catch(() => {
          const remaining = Math.max(0, (snapshot.expiresAt ?? Date.now()) - Date.now());
          expiryTimer = setTimeout(expireAuthSession, remaining);
        });
      },
      Math.min(Math.max(0, refreshAt - Date.now()), 2_147_483_647),
    );
    return () => {
      clearTimeout(refreshTimer);
      if (expiryTimer) clearTimeout(expiryTimer);
    };
  }, [snapshot.canRefresh, snapshot.expiresAt, snapshot.status, testStatus]);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...snapshot,
      isAuthenticated: authenticated(snapshot.status),
      beginLogin: beginOidcLogin,
      completeLogin: completeOidcLogin,
      logout: logoutOidc,
    }),
    [snapshot],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// Context hooks conventionally live beside their provider.
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth requires an <AuthProvider> ancestor");
  return value;
}

export function RequireAuthentication({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (
      auth.status !== "anonymous" &&
      auth.status !== "expired" &&
      auth.status !== "configuration-error"
    ) {
      return;
    }
    // The protected parent can remain mounted for a render while Router commits
    // the sibling login route. Never turn the login URL into its own returnTo.
    if (location.pathname === "/login") return;
    void navigate({
      to: "/login",
      search: {
        returnTo: `${location.pathname}${location.searchStr}`,
        reason:
          auth.status === "expired"
            ? "expired"
            : auth.status === "configuration-error"
              ? "configuration"
              : undefined,
      },
      replace: true,
    });
  }, [auth.status, location.pathname, location.searchStr, navigate]);

  if (auth.status === "loading") {
    return (
      <main
        role="status"
        style={{
          minHeight: "50vh",
          display: "grid",
          placeContent: "center",
          justifyItems: "center",
          gap: "var(--soa-space-3)",
        }}
      >
        <Spinner label="Restoring session" />
        <span>Restoring your secure session…</span>
      </main>
    );
  }

  if (!auth.isAuthenticated) {
    return (
      <main style={{ maxWidth: "32rem", margin: "8vh auto", padding: "0 var(--soa-space-6)" }}>
        <Banner tone="info" title="Sign-in required">
          Taking you to the secure sign-in page…
        </Banner>
      </main>
    );
  }

  return children;
}
