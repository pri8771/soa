import { env } from "../env";
import { firebaseAuthClient } from "./firebase";
import {
  beginLogin as beginOidcLogin,
  bootstrapAuth as bootstrapOidc,
  completeLogin as completeOidcLogin,
  expireAuthSession as expireOidc,
  forceRefreshBearerToken as refreshOidc,
  getAuthSnapshot as getOidcSnapshot,
  getBearerToken as getOidcBearerToken,
  logout as logoutOidc,
  subscribeAuth as subscribeOidc,
  type AuthSnapshot,
  type AuthStatus,
} from "./oidc";

export type { AuthSnapshot, AuthStatus };

function usesFirebase(): boolean {
  // Tests keep their deterministic in-memory session, but an explicit
  // Firebase selection in local development must exercise the real SDK so
  // redirect domains, tenant IDs, and provider configuration can be proven
  // before release.
  return env.MODE !== "test" && env.VITE_AUTH_MODE === "firebase";
}

const missingFirebase: AuthSnapshot = {
  status: "configuration-error",
  expiresAt: null,
  canRefresh: false,
  error: "Firebase authentication is not completely configured for this deployment.",
};

export function getAuthSnapshot(): AuthSnapshot {
  if (!usesFirebase()) return getOidcSnapshot();
  return firebaseAuthClient?.getSnapshot() ?? missingFirebase;
}

export function subscribeAuth(listener: () => void): () => void {
  if (!usesFirebase()) return subscribeOidc(listener);
  return firebaseAuthClient?.subscribe(listener) ?? (() => undefined);
}

export async function bootstrapAuth(): Promise<void> {
  if (!usesFirebase()) return bootstrapOidc();
  await firebaseAuthClient?.bootstrap();
}

export async function beginLogin(returnTo?: string): Promise<void> {
  if (!usesFirebase()) return beginOidcLogin(returnTo);
  if (!firebaseAuthClient) throw new Error(missingFirebase.error ?? "Authentication unavailable.");
  await firebaseAuthClient.beginLogin(returnTo);
}

export function completeLogin(callbackUrl: string): Promise<string> {
  if (!usesFirebase()) return completeOidcLogin(callbackUrl);
  return Promise.resolve(firebaseAuthClient?.consumeReturnTo() ?? "/select-organization");
}

export function consumePostLoginDestination(): string | null {
  return usesFirebase() ? (firebaseAuthClient?.consumeReturnTo() ?? null) : null;
}

export function getBearerToken(): Promise<string | null> {
  return usesFirebase()
    ? (firebaseAuthClient?.getBearerToken() ?? Promise.resolve(null))
    : getOidcBearerToken();
}

export function forceRefreshBearerToken(): Promise<string> {
  if (!usesFirebase()) return refreshOidc();
  if (!firebaseAuthClient) return Promise.reject(new Error("Authentication unavailable."));
  return firebaseAuthClient.forceRefreshBearerToken();
}

export function expireAuthSession(): void {
  if (!usesFirebase()) {
    expireOidc();
    return;
  }
  firebaseAuthClient?.expire();
}

export function logout(): Promise<void> {
  return usesFirebase() ? (firebaseAuthClient?.logout() ?? Promise.resolve()) : logoutOidc();
}
