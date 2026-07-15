import { env } from "../env";

const SESSION_KEY = "soa.auth.oidc.session";
const TRANSACTION_KEY = "soa.auth.oidc.transaction";
const TRANSACTION_TTL_MS = 10 * 60 * 1_000;
const REFRESH_WINDOW_MS = 60 * 1_000;

export type AuthStatus =
  | "loading"
  | "authenticated"
  | "anonymous"
  | "expired"
  | "development"
  | "test"
  | "configuration-error";

export interface AuthSnapshot {
  status: AuthStatus;
  expiresAt: number | null;
  canRefresh: boolean;
  error: string | null;
}

export interface OidcConfig {
  issuer: string;
  clientId: string;
  scope: string;
  audience?: string;
  bearerToken: "id_token" | "access_token";
}

interface OidcDiscoveryDocument {
  issuer: string;
  authorization_endpoint: string;
  token_endpoint: string;
  end_session_endpoint?: string;
  code_challenge_methods_supported?: string[];
}

interface OidcTokenResponse {
  access_token?: string;
  id_token?: string;
  refresh_token?: string;
  token_type?: string;
  expires_in?: number;
}

interface StoredSession {
  accessToken: string | null;
  idToken: string | null;
  refreshToken: string | null;
  expiresAt: number;
}

interface AuthorizationTransaction {
  state: string;
  nonce: string;
  verifier: string;
  returnTo: string;
  redirectUri: string;
  createdAt: number;
}

interface JwtClaims {
  iss?: unknown;
  aud?: unknown;
  exp?: unknown;
  nonce?: unknown;
}

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface OidcClientDependencies {
  fetch?: Fetcher;
  storage?: Storage;
  crypto?: Crypto;
  now?: () => number;
  location?: { origin: string; assign(url: string): void };
}

export class OidcError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "OidcError";
  }
}

function normalizeIssuer(value: string): string {
  return value.replace(/\/+$/, "");
}

function isSecureEndpoint(value: string): boolean {
  const url = new URL(value);
  return (
    url.protocol === "https:" ||
    (url.protocol === "http:" && (url.hostname === "localhost" || url.hostname === "127.0.0.1"))
  );
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomValue(cryptoProvider: Crypto, byteLength = 32): string {
  const bytes = new Uint8Array(byteLength);
  cryptoProvider.getRandomValues(bytes);
  return base64Url(bytes);
}

async function pkceChallenge(cryptoProvider: Crypto, verifier: string): Promise<string> {
  const digest = await cryptoProvider.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

function parseJwtClaims(token: string): JwtClaims {
  const segments = token.split(".");
  if (segments.length !== 3 || !segments[1]) {
    throw new OidcError("invalid_id_token", "The identity provider returned an invalid ID token.");
  }
  try {
    const value = segments[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = value.padEnd(Math.ceil(value.length / 4) * 4, "=");
    return JSON.parse(atob(padded)) as JwtClaims;
  } catch {
    throw new OidcError("invalid_id_token", "The identity provider returned an invalid ID token.");
  }
}

function audienceIncludes(value: unknown, expected: string): boolean {
  return value === expected || (Array.isArray(value) && value.includes(expected));
}

function safeReturnTo(value: string | null | undefined): string {
  if (
    !value ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value === "/login" ||
    value.startsWith("/login?")
  ) {
    return "/select-organization";
  }
  return value;
}

function readJson<T>(storage: Storage, key: string): T | null {
  const serialized = storage.getItem(key);
  if (!serialized) return null;
  try {
    return JSON.parse(serialized) as T;
  } catch {
    storage.removeItem(key);
    return null;
  }
}

function parseTokenErrorBody(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const body = value as { error_description?: unknown; error?: unknown };
  if (typeof body.error_description === "string") return body.error_description;
  if (typeof body.error === "string") return body.error;
  return null;
}

export class OidcClient {
  private readonly fetcher: Fetcher;
  private readonly storage: Storage;
  private readonly cryptoProvider: Crypto;
  private readonly now: () => number;
  private readonly browserLocation: { origin: string; assign(url: string): void };
  private readonly listeners = new Set<() => void>();
  private discoveryPromise: Promise<OidcDiscoveryDocument> | null = null;
  private refreshPromise: Promise<string> | null = null;
  private snapshot: AuthSnapshot = {
    status: "loading",
    expiresAt: null,
    canRefresh: false,
    error: null,
  };

  constructor(
    private readonly config: OidcConfig | null,
    dependencies: OidcClientDependencies = {},
  ) {
    this.fetcher = dependencies.fetch ?? globalThis.fetch.bind(globalThis);
    this.storage = dependencies.storage ?? globalThis.sessionStorage;
    this.cryptoProvider = dependencies.crypto ?? globalThis.crypto;
    this.now = dependencies.now ?? Date.now;
    this.browserLocation = dependencies.location ?? globalThis.location;
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

  private requireConfig(): OidcConfig {
    if (!this.config) {
      throw new OidcError(
        "configuration_missing",
        "Single sign-on is not configured for this deployment. Contact an administrator.",
      );
    }
    if (!isSecureEndpoint(this.config.issuer)) {
      throw new OidcError("insecure_issuer", "The configured identity provider must use HTTPS.");
    }
    return this.config;
  }

  private readSession(): StoredSession | null {
    const session = readJson<StoredSession>(this.storage, SESSION_KEY);
    if (
      !session ||
      typeof session.expiresAt !== "number" ||
      (!session.accessToken && !session.idToken)
    ) {
      if (session) this.storage.removeItem(SESSION_KEY);
      return null;
    }
    return session;
  }

  private writeSession(session: StoredSession): void {
    this.storage.setItem(SESSION_KEY, JSON.stringify(session));
    this.publish({
      status: "authenticated",
      expiresAt: session.expiresAt,
      canRefresh: Boolean(session.refreshToken),
      error: null,
    });
  }

  clear(status: "anonymous" | "expired" = "anonymous"): void {
    this.storage.removeItem(SESSION_KEY);
    this.storage.removeItem(TRANSACTION_KEY);
    this.publish({ status, expiresAt: null, canRefresh: false, error: null });
  }

  private async discover(): Promise<OidcDiscoveryDocument> {
    if (this.discoveryPromise) return this.discoveryPromise;
    const config = this.requireConfig();
    this.discoveryPromise = (async () => {
      const response = await this.fetcher(
        `${normalizeIssuer(config.issuer)}/.well-known/openid-configuration`,
        { headers: { Accept: "application/json" } },
      );
      if (!response.ok) {
        throw new OidcError(
          "discovery_failed",
          "The identity provider configuration could not be loaded.",
        );
      }
      const document = (await response.json()) as Partial<OidcDiscoveryDocument>;
      if (
        normalizeIssuer(document.issuer ?? "") !== normalizeIssuer(config.issuer) ||
        !document.authorization_endpoint ||
        !document.token_endpoint ||
        !isSecureEndpoint(document.authorization_endpoint) ||
        !isSecureEndpoint(document.token_endpoint)
      ) {
        throw new OidcError(
          "invalid_discovery",
          "The identity provider published an invalid OIDC configuration.",
        );
      }
      if (document.end_session_endpoint && !isSecureEndpoint(document.end_session_endpoint)) {
        throw new OidcError(
          "invalid_discovery",
          "The identity provider published an invalid logout endpoint.",
        );
      }
      if (
        document.code_challenge_methods_supported &&
        !document.code_challenge_methods_supported.includes("S256")
      ) {
        throw new OidcError(
          "pkce_unsupported",
          "The identity provider does not support the required PKCE S256 flow.",
        );
      }
      return document as OidcDiscoveryDocument;
    })().catch((error: unknown) => {
      this.discoveryPromise = null;
      throw error;
    });
    return this.discoveryPromise;
  }

  async bootstrap(): Promise<void> {
    if (!this.config) {
      this.publish({
        status: "configuration-error",
        expiresAt: null,
        canRefresh: false,
        error: "Single sign-on is not configured for this deployment.",
      });
      return;
    }
    const session = this.readSession();
    if (!session) {
      this.publish({ status: "anonymous", expiresAt: null, canRefresh: false, error: null });
      return;
    }
    if (session.expiresAt > this.now()) {
      this.writeSession(session);
      if (session.refreshToken && session.expiresAt <= this.now() + REFRESH_WINDOW_MS) {
        try {
          await this.refreshBearerToken();
        } catch {
          if (session.expiresAt <= this.now()) this.clear("expired");
        }
      }
      return;
    }
    if (session.refreshToken) {
      try {
        await this.refreshBearerToken();
        return;
      } catch {
        // A stale session must never leave protected routes unlocked.
      }
    }
    this.clear("expired");
  }

  async createAuthorizationRequest(returnTo?: string): Promise<string> {
    const config = this.requireConfig();
    const discovery = await this.discover();
    const verifier = randomValue(this.cryptoProvider, 48);
    const transaction: AuthorizationTransaction = {
      state: randomValue(this.cryptoProvider),
      nonce: randomValue(this.cryptoProvider),
      verifier,
      returnTo: safeReturnTo(returnTo),
      redirectUri: `${this.browserLocation.origin}/login`,
      createdAt: this.now(),
    };
    this.storage.setItem(TRANSACTION_KEY, JSON.stringify(transaction));

    const authorizationUrl = new URL(discovery.authorization_endpoint);
    authorizationUrl.searchParams.set("response_type", "code");
    authorizationUrl.searchParams.set("client_id", config.clientId);
    authorizationUrl.searchParams.set("redirect_uri", transaction.redirectUri);
    authorizationUrl.searchParams.set("scope", config.scope);
    authorizationUrl.searchParams.set("state", transaction.state);
    authorizationUrl.searchParams.set("nonce", transaction.nonce);
    authorizationUrl.searchParams.set(
      "code_challenge",
      await pkceChallenge(this.cryptoProvider, verifier),
    );
    authorizationUrl.searchParams.set("code_challenge_method", "S256");
    if (config.audience) authorizationUrl.searchParams.set("audience", config.audience);
    return authorizationUrl.toString();
  }

  async beginAuthorization(returnTo?: string): Promise<void> {
    this.browserLocation.assign(await this.createAuthorizationRequest(returnTo));
  }

  private validateIdToken(idToken: string, nonce?: string): JwtClaims {
    const config = this.requireConfig();
    const claims = parseJwtClaims(idToken);
    if (normalizeIssuer(String(claims.iss ?? "")) !== normalizeIssuer(config.issuer)) {
      throw new OidcError(
        "issuer_mismatch",
        "The identity response came from an unexpected issuer.",
      );
    }
    if (!audienceIncludes(claims.aud, config.clientId)) {
      throw new OidcError(
        "audience_mismatch",
        "The identity response was issued for a different application.",
      );
    }
    if (nonce !== undefined && claims.nonce !== nonce) {
      throw new OidcError("nonce_mismatch", "The identity response could not be verified.");
    }
    return claims;
  }

  private sessionFromTokenResponse(
    response: OidcTokenResponse,
    previous: StoredSession | null,
    idTokenClaims?: JwtClaims,
  ): StoredSession {
    const config = this.requireConfig();
    if (response.token_type && response.token_type.toLowerCase() !== "bearer") {
      throw new OidcError(
        "invalid_token_type",
        "The identity provider returned an unsupported token type.",
      );
    }
    const accessToken = response.access_token ?? null;
    const idToken = response.id_token ?? null;
    const bearer = config.bearerToken === "id_token" ? idToken : accessToken;
    if (!bearer) {
      throw new OidcError(
        "missing_bearer_token",
        `The identity provider did not return the configured ${config.bearerToken.replace("_", " ")}.`,
      );
    }
    const claims = idTokenClaims ?? (idToken ? this.validateIdToken(idToken) : undefined);
    const idTokenExpiry = typeof claims?.exp === "number" ? claims.exp * 1_000 : null;
    const responseExpiry =
      typeof response.expires_in === "number" && response.expires_in > 0
        ? this.now() + response.expires_in * 1_000
        : null;
    let accessTokenExpiry: number | null = null;
    if (accessToken?.split(".").length === 3) {
      try {
        const accessClaims = parseJwtClaims(accessToken);
        accessTokenExpiry = typeof accessClaims.exp === "number" ? accessClaims.exp * 1_000 : null;
      } catch {
        // Opaque access tokens are valid; their expires_in value is authoritative.
      }
    }
    const expiresAt =
      config.bearerToken === "id_token"
        ? (idTokenExpiry ?? responseExpiry)
        : (responseExpiry ?? accessTokenExpiry);
    if (!expiresAt || expiresAt <= this.now()) {
      throw new OidcError("invalid_expiry", "The identity provider returned an expired token.");
    }
    return {
      accessToken,
      idToken,
      refreshToken: response.refresh_token ?? previous?.refreshToken ?? null,
      expiresAt,
    };
  }

  private async requestTokens(body: URLSearchParams): Promise<OidcTokenResponse> {
    const discovery = await this.discover();
    const response = await this.fetcher(discovery.token_endpoint, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      // Keep provider response bodies out of the UI; only safe OAuth fields are surfaced.
    }
    if (!response.ok) {
      throw new OidcError(
        "token_exchange_failed",
        parseTokenErrorBody(payload) ?? "The identity provider rejected the session request.",
      );
    }
    if (!payload || typeof payload !== "object") {
      throw new OidcError(
        "invalid_token_response",
        "The identity provider returned an invalid token response.",
      );
    }
    return payload as OidcTokenResponse;
  }

  async completeAuthorization(callbackUrl: string): Promise<string> {
    const config = this.requireConfig();
    const callback = new URL(callbackUrl);
    const providerError = callback.searchParams.get("error");
    if (providerError) {
      this.storage.removeItem(TRANSACTION_KEY);
      throw new OidcError(
        providerError,
        callback.searchParams.get("error_description") ?? "Sign-in was not completed.",
      );
    }
    const transaction = readJson<AuthorizationTransaction>(this.storage, TRANSACTION_KEY);
    this.storage.removeItem(TRANSACTION_KEY);
    const code = callback.searchParams.get("code");
    const state = callback.searchParams.get("state");
    if (!transaction || !code || !state || state !== transaction.state) {
      throw new OidcError("invalid_callback", "The sign-in callback could not be verified.");
    }
    if (this.now() - transaction.createdAt > TRANSACTION_TTL_MS) {
      throw new OidcError("expired_callback", "The sign-in attempt expired. Start again.");
    }
    const tokenResponse = await this.requestTokens(
      new URLSearchParams({
        grant_type: "authorization_code",
        code,
        client_id: config.clientId,
        redirect_uri: transaction.redirectUri,
        code_verifier: transaction.verifier,
      }),
    );
    if (!tokenResponse.id_token) {
      throw new OidcError("missing_id_token", "The identity provider did not return an ID token.");
    }
    const claims = this.validateIdToken(tokenResponse.id_token, transaction.nonce);
    this.writeSession(this.sessionFromTokenResponse(tokenResponse, null, claims));
    return transaction.returnTo;
  }

  private bearerFromSession(session: StoredSession): string | null {
    if (!this.config) return session.idToken ?? session.accessToken;
    return this.config.bearerToken === "id_token" ? session.idToken : session.accessToken;
  }

  async getBearerToken(): Promise<string | null> {
    const session = this.readSession();
    if (!session) return null;
    if (session.expiresAt <= this.now()) {
      if (session.refreshToken) return this.refreshBearerToken();
      this.clear("expired");
      return null;
    }
    if (session.refreshToken && session.expiresAt <= this.now() + REFRESH_WINDOW_MS) {
      return this.refreshBearerToken();
    }
    return this.bearerFromSession(session);
  }

  async refreshBearerToken(): Promise<string> {
    if (this.refreshPromise) return this.refreshPromise;
    const session = this.readSession();
    if (!session?.refreshToken) {
      this.clear("expired");
      throw new OidcError("session_expired", "Your session has expired. Sign in again.");
    }
    const config = this.requireConfig();
    this.refreshPromise = (async () => {
      const tokenResponse = await this.requestTokens(
        new URLSearchParams({
          grant_type: "refresh_token",
          refresh_token: session.refreshToken ?? "",
          client_id: config.clientId,
        }),
      );
      const refreshed = this.sessionFromTokenResponse(tokenResponse, session);
      this.writeSession(refreshed);
      const bearer = this.bearerFromSession(refreshed);
      if (!bearer) throw new OidcError("missing_bearer_token", "The refreshed session is invalid.");
      return bearer;
    })().finally(() => {
      this.refreshPromise = null;
    });
    return this.refreshPromise;
  }

  expire(): void {
    this.clear("expired");
  }

  async logout(): Promise<void> {
    const session = this.readSession();
    this.clear("anonymous");
    const localLogin = `${this.browserLocation.origin}/login`;
    try {
      const discovery = await this.discover();
      if (discovery.end_session_endpoint) {
        const logoutUrl = new URL(discovery.end_session_endpoint);
        if (session?.idToken) logoutUrl.searchParams.set("id_token_hint", session.idToken);
        logoutUrl.searchParams.set("post_logout_redirect_uri", localLogin);
        this.browserLocation.assign(logoutUrl.toString());
        return;
      }
    } catch {
      // Local session destruction is authoritative even if provider logout is unavailable.
    }
    this.browserLocation.assign(localLogin);
  }

  /** Test-only token installation; production callers cannot invoke it. */
  installTestSession(session: StoredSession): void {
    this.writeSession(session);
  }
}

const runtimeConfig: OidcConfig | null =
  env.VITE_OIDC_ISSUER && env.VITE_OIDC_CLIENT_ID
    ? {
        issuer: env.VITE_OIDC_ISSUER,
        clientId: env.VITE_OIDC_CLIENT_ID,
        scope: env.VITE_OIDC_SCOPE,
        audience: env.VITE_OIDC_AUDIENCE,
        bearerToken: env.VITE_OIDC_BEARER_TOKEN,
      }
    : null;

const runtimeClient = new OidcClient(runtimeConfig);
const developmentSnapshot: AuthSnapshot = {
  status: "development",
  expiresAt: null,
  canRefresh: false,
  error: null,
};
const testSnapshot: AuthSnapshot = {
  status: "test",
  expiresAt: null,
  canRefresh: false,
  error: null,
};

function usesDevelopmentIdentity(): boolean {
  return env.MODE === "development" && env.VITE_AUTH_MODE === undefined;
}

function usesRuntimeOidc(): boolean {
  return env.MODE !== "test" && !usesDevelopmentIdentity();
}

export function getAuthSnapshot(): AuthSnapshot {
  if (usesDevelopmentIdentity()) return developmentSnapshot;
  if (env.MODE === "test" && runtimeClient.getSnapshot().status === "loading") return testSnapshot;
  return runtimeClient.getSnapshot();
}

export function subscribeAuth(listener: () => void): () => void {
  if (!usesRuntimeOidc() && runtimeClient.getSnapshot().status === "loading")
    return () => undefined;
  return runtimeClient.subscribe(listener);
}

export async function bootstrapAuth(): Promise<void> {
  if (!usesRuntimeOidc()) return;
  await runtimeClient.bootstrap();
}

export function beginLogin(returnTo?: string): Promise<void> {
  return runtimeClient.beginAuthorization(returnTo);
}

export function completeLogin(callbackUrl: string): Promise<string> {
  return runtimeClient.completeAuthorization(callbackUrl);
}

export function getBearerToken(): Promise<string | null> {
  return runtimeClient.getBearerToken();
}

export function forceRefreshBearerToken(): Promise<string> {
  return runtimeClient.refreshBearerToken();
}

export function expireAuthSession(): void {
  if (usesDevelopmentIdentity()) return;
  if (env.MODE === "test" && runtimeClient.getSnapshot().status === "loading") return;
  runtimeClient.expire();
}

export function logout(): Promise<void> {
  return runtimeClient.logout();
}

export function sanitizeReturnTo(value: string | null | undefined): string {
  return safeReturnTo(value);
}

export function installAuthSessionForTests(input: {
  accessToken?: string;
  idToken?: string;
  refreshToken?: string;
  expiresAt?: number;
}): void {
  if (env.MODE !== "test") throw new Error("Test sessions can only be installed in test mode.");
  runtimeClient.installTestSession({
    accessToken: input.accessToken ?? null,
    idToken: input.idToken ?? null,
    refreshToken: input.refreshToken ?? null,
    expiresAt: input.expiresAt ?? Date.now() + 5 * 60 * 1_000,
  });
}

export function resetAuthSessionForTests(): void {
  if (env.MODE !== "test") throw new Error("Test sessions can only be reset in test mode.");
  runtimeClient.clear("anonymous");
}
