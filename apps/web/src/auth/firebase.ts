import type { FirebaseApp } from "firebase/app";
import type { Auth, AuthProvider as FirebaseProvider, User } from "firebase/auth";

import { env } from "../env";
import { sanitizeReturnTo, type AuthSnapshot } from "./oidc";

const RETURN_TO_KEY = "soa.auth.firebase.return-to";

export interface FirebaseBrowserConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
  tenantId?: string;
  providerId: string;
  loginMethod: "redirect" | "popup";
}

interface FirebaseSdk {
  app: typeof import("firebase/app");
  auth: typeof import("firebase/auth");
}

interface FirebaseClientDependencies {
  loadSdk?: () => Promise<FirebaseSdk>;
  storage?: Storage;
}

function safeMessage(error: unknown): string {
  if (error && typeof error === "object" && "code" in error) {
    const code = String((error as { code: unknown }).code);
    if (code === "auth/popup-closed-by-user") return "The sign-in window was closed.";
    if (code === "auth/account-exists-with-different-credential") {
      return "This account must use its original sign-in provider.";
    }
    if (code === "auth/unauthorized-domain") {
      return "This application domain is not authorized by the identity provider.";
    }
  }
  return "The identity provider could not complete sign-in.";
}

function errorCode(error: unknown): string | null {
  return error && typeof error === "object" && "code" in error
    ? String((error as { code: unknown }).code)
    : null;
}

export class FirebaseAuthClient {
  private readonly loadSdk: () => Promise<FirebaseSdk>;
  private readonly storage: Storage;
  private sdk: FirebaseSdk | null = null;
  private auth: Auth | null = null;
  private initialization: Promise<Auth> | null = null;
  private bootstrapPromise: Promise<void> | null = null;
  private unsubscribe: (() => void) | null = null;
  private forcedExpired = false;
  private readonly listeners = new Set<() => void>();
  private snapshot: AuthSnapshot = {
    status: "loading",
    expiresAt: null,
    canRefresh: true,
    error: null,
  };

  constructor(
    private readonly config: FirebaseBrowserConfig,
    dependencies: FirebaseClientDependencies = {},
  ) {
    this.loadSdk =
      dependencies.loadSdk ??
      (async () => ({ app: await import("firebase/app"), auth: await import("firebase/auth") }));
    this.storage = dependencies.storage ?? globalThis.sessionStorage;
  }

  getSnapshot = (): AuthSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private publish(snapshot: AuthSnapshot): void {
    this.snapshot = snapshot;
    for (const listener of this.listeners) listener();
  }

  private async ensureAuth(): Promise<Auth> {
    if (this.auth) return this.auth;
    if (this.initialization) return this.initialization;
    this.initialization = (async () => {
      const sdk = await this.loadSdk();
      this.sdk = sdk;
      const existing = sdk.app.getApps().find((candidate) => candidate.name === "soa-web");
      const app: FirebaseApp =
        existing ??
        sdk.app.initializeApp(
          {
            apiKey: this.config.apiKey,
            authDomain: this.config.authDomain,
            projectId: this.config.projectId,
            appId: this.config.appId,
          },
          "soa-web",
        );
      const auth = sdk.auth.getAuth(app);
      if (this.config.tenantId) auth.tenantId = this.config.tenantId;
      await sdk.auth.setPersistence(auth, sdk.auth.browserSessionPersistence);
      this.auth = auth;
      return auth;
    })();
    return this.initialization;
  }

  private provider(sdk: FirebaseSdk): FirebaseProvider {
    const provider = new sdk.auth.OAuthProvider(this.config.providerId);
    provider.setCustomParameters({ prompt: "select_account" });
    return provider;
  }

  private async publishUser(user: User | null): Promise<void> {
    if (!user) {
      this.publish({
        status: this.forcedExpired ? "expired" : "anonymous",
        expiresAt: null,
        canRefresh: true,
        error: null,
      });
      return;
    }
    const result = await user.getIdTokenResult(false);
    this.publish({
      status: "authenticated",
      expiresAt: Date.parse(result.expirationTime),
      canRefresh: true,
      error: null,
    });
  }

  async bootstrap(): Promise<void> {
    if (this.bootstrapPromise) return this.bootstrapPromise;
    this.bootstrapPromise = (async () => {
      try {
        const auth = await this.ensureAuth();
        const sdk = this.sdk;
        if (!sdk) throw new Error("Firebase SDK did not initialize.");

        let resolveInitial: (() => void) | null = null;
        const initial = new Promise<void>((resolve) => {
          resolveInitial = resolve;
        });
        this.unsubscribe?.();
        this.unsubscribe = sdk.auth.onIdTokenChanged(auth, (user) => {
          void this.publishUser(user).finally(() => {
            resolveInitial?.();
            resolveInitial = null;
          });
        });
        await sdk.auth.getRedirectResult(auth);
        await initial;
      } catch (error) {
        this.publish({
          status: "anonymous",
          expiresAt: null,
          canRefresh: true,
          error: safeMessage(error),
        });
      }
    })();
    return this.bootstrapPromise;
  }

  async beginLogin(returnTo?: string): Promise<void> {
    const auth = await this.ensureAuth();
    const sdk = this.sdk;
    if (!sdk) throw new Error("Firebase SDK did not initialize.");
    this.forcedExpired = false;
    this.storage.setItem(RETURN_TO_KEY, sanitizeReturnTo(returnTo));
    const provider = this.provider(sdk);
    if (this.config.loginMethod === "popup") {
      try {
        const result = await sdk.auth.signInWithPopup(auth, provider);
        await this.publishUser(result.user);
        return;
      } catch (error) {
        const code = errorCode(error);
        if (code !== "auth/popup-blocked" && code !== "auth/cancelled-popup-request") {
          throw new Error(safeMessage(error));
        }
      }
    }
    await sdk.auth.signInWithRedirect(auth, provider);
  }

  async getBearerToken(): Promise<string | null> {
    await this.bootstrap();
    const user = this.auth?.currentUser;
    return user ? user.getIdToken(false) : null;
  }

  async forceRefreshBearerToken(): Promise<string> {
    await this.bootstrap();
    const user = this.auth?.currentUser;
    if (!user) throw new Error("Your session has expired. Sign in again.");
    const token = await user.getIdToken(true);
    await this.publishUser(user);
    return token;
  }

  expire(): void {
    this.forcedExpired = true;
    this.publish({ status: "expired", expiresAt: null, canRefresh: true, error: null });
    if (this.auth && this.sdk) void this.sdk.auth.signOut(this.auth);
  }

  async logout(): Promise<void> {
    this.forcedExpired = false;
    this.storage.removeItem(RETURN_TO_KEY);
    const auth = await this.ensureAuth();
    const sdk = this.sdk;
    if (sdk) await sdk.auth.signOut(auth);
    this.publish({ status: "anonymous", expiresAt: null, canRefresh: true, error: null });
  }

  consumeReturnTo(): string | null {
    const value = this.storage.getItem(RETURN_TO_KEY);
    this.storage.removeItem(RETURN_TO_KEY);
    return value ? sanitizeReturnTo(value) : null;
  }
}

export const firebaseConfig: FirebaseBrowserConfig | null =
  env.VITE_FIREBASE_API_KEY &&
  env.VITE_FIREBASE_AUTH_DOMAIN &&
  env.VITE_FIREBASE_PROJECT_ID &&
  env.VITE_FIREBASE_APP_ID &&
  env.VITE_FIREBASE_PROVIDER_ID
    ? {
        apiKey: env.VITE_FIREBASE_API_KEY,
        authDomain: env.VITE_FIREBASE_AUTH_DOMAIN,
        projectId: env.VITE_FIREBASE_PROJECT_ID,
        appId: env.VITE_FIREBASE_APP_ID,
        tenantId: env.VITE_FIREBASE_TENANT_ID,
        providerId: env.VITE_FIREBASE_PROVIDER_ID,
        loginMethod: env.VITE_FIREBASE_LOGIN_METHOD,
      }
    : null;

export const firebaseAuthClient = firebaseConfig ? new FirebaseAuthClient(firebaseConfig) : null;
