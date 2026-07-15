import { OidcClient, OidcError, sanitizeReturnTo, type OidcConfig } from "./oidc";

const ISSUER = "https://identity.example.com";
const CONFIG: OidcConfig = {
  issuer: ISSUER,
  clientId: "soa-web",
  scope: "openid profile email offline_access",
  audience: "soa-api",
  bearerToken: "id_token",
};
const DISCOVERY = {
  issuer: ISSUER,
  authorization_endpoint: `${ISSUER}/authorize`,
  token_endpoint: `${ISSUER}/token`,
  end_session_endpoint: `${ISSUER}/logout`,
  code_challenge_methods_supported: ["S256"],
};

function encoded(value: object): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function idToken(payload: object): string {
  return `${encoded({ alg: "RS256", typ: "JWT" })}.${encoded(payload)}.signature`;
}

function makeLocation() {
  return { origin: "https://app.example.com", assign: vi.fn() };
}

describe("OidcClient", () => {
  beforeEach(() => sessionStorage.clear());

  it("builds a state, nonce, and S256 PKCE authorization request", async () => {
    const fetcher = vi.fn(async () => HttpResponse(DISCOVERY));
    const client = new OidcClient(CONFIG, { fetch: fetcher, location: makeLocation() });

    const request = new URL(await client.createAuthorizationRequest("/app/northstar/review"));

    expect(request.origin + request.pathname).toBe(`${ISSUER}/authorize`);
    expect(request.searchParams.get("response_type")).toBe("code");
    expect(request.searchParams.get("client_id")).toBe("soa-web");
    expect(request.searchParams.get("redirect_uri")).toBe("https://app.example.com/login");
    expect(request.searchParams.get("audience")).toBe("soa-api");
    expect(request.searchParams.get("state")).toHaveLength(43);
    expect(request.searchParams.get("nonce")).toHaveLength(43);
    expect(request.searchParams.get("code_challenge_method")).toBe("S256");
    expect(request.searchParams.get("code_challenge")).toMatch(/^[\w-]{43}$/);
    expect(request.searchParams.has("code_verifier")).toBe(false);
  });

  it("exchanges a verified callback and returns the internal destination", async () => {
    const now = Date.UTC(2026, 6, 15, 12);
    let nonce = "";
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("openid-configuration")) return HttpResponse(DISCOVERY);
      expect(String(init?.body)).toContain("grant_type=authorization_code");
      return HttpResponse({
        access_token: "opaque-access-token",
        id_token: idToken({
          iss: ISSUER,
          aud: CONFIG.clientId,
          nonce,
          exp: now / 1_000 + 3_600,
        }),
        refresh_token: "refresh-1",
        token_type: "Bearer",
        expires_in: 3_600,
      });
    });
    const client = new OidcClient(CONFIG, {
      fetch: fetcher,
      location: makeLocation(),
      now: () => now,
    });
    const request = new URL(await client.createAuthorizationRequest("/app/northstar/review"));
    nonce = request.searchParams.get("nonce") ?? "";

    const destination = await client.completeAuthorization(
      `https://app.example.com/login?code=code-1&state=${request.searchParams.get("state")}`,
    );

    expect(destination).toBe("/app/northstar/review");
    expect(await client.getBearerToken()).toContain(".signature");
    expect(client.getSnapshot()).toMatchObject({ status: "authenticated", canRefresh: true });
  });

  it("single-flights refreshes near expiry", async () => {
    let now = Date.UTC(2026, 6, 15, 12);
    let nonce = "";
    let refreshRequests = 0;
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("openid-configuration")) return HttpResponse(DISCOVERY);
      const body = String(init?.body);
      if (body.includes("grant_type=refresh_token")) {
        refreshRequests += 1;
        return HttpResponse({
          access_token: "access-2",
          id_token: idToken({ iss: ISSUER, aud: CONFIG.clientId, exp: now / 1_000 + 3_600 }),
          refresh_token: "refresh-2",
          token_type: "Bearer",
          expires_in: 3_600,
        });
      }
      return HttpResponse({
        access_token: "access-1",
        id_token: idToken({
          iss: ISSUER,
          aud: CONFIG.clientId,
          nonce,
          exp: now / 1_000 + 3_600,
        }),
        refresh_token: "refresh-1",
        token_type: "Bearer",
        expires_in: 3_600,
      });
    });
    const client = new OidcClient(CONFIG, {
      fetch: fetcher,
      location: makeLocation(),
      now: () => now,
    });
    const request = new URL(await client.createAuthorizationRequest());
    nonce = request.searchParams.get("nonce") ?? "";
    await client.completeAuthorization(
      `https://app.example.com/login?code=code-1&state=${request.searchParams.get("state")}`,
    );
    now += 3_550_000;

    const [first, second] = await Promise.all([client.getBearerToken(), client.getBearerToken()]);

    expect(first).toBe(second);
    expect(refreshRequests).toBe(1);
    expect(client.getSnapshot().expiresAt).toBe(now + 3_600_000);
  });

  it("rejects a callback whose state does not match without exchanging it", async () => {
    const fetcher = vi.fn(async () => HttpResponse(DISCOVERY));
    const client = new OidcClient(CONFIG, { fetch: fetcher, location: makeLocation() });
    await client.createAuthorizationRequest();

    await expect(
      client.completeAuthorization("https://app.example.com/login?code=code-1&state=attacker"),
    ).rejects.toEqual(expect.objectContaining<Partial<OidcError>>({ code: "invalid_callback" }));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("expires a stored session that cannot be refreshed", async () => {
    const now = Date.UTC(2026, 6, 15, 12);
    const client = new OidcClient(CONFIG, {
      fetch: vi.fn(),
      location: makeLocation(),
      now: () => now,
    });
    client.installTestSession({
      accessToken: "access",
      idToken: "id",
      refreshToken: null,
      expiresAt: now - 1,
    });

    await expect(client.getBearerToken()).resolves.toBeNull();
    expect(client.getSnapshot().status).toBe("expired");
  });

  it("clears local credentials before redirecting through provider logout", async () => {
    const location = makeLocation();
    const client = new OidcClient(CONFIG, {
      fetch: vi.fn(async () => HttpResponse(DISCOVERY)),
      location,
    });
    client.installTestSession({
      accessToken: "access",
      idToken: "id-token-hint",
      refreshToken: "refresh",
      expiresAt: Date.now() + 60_000,
    });

    await client.logout();

    await expect(client.getBearerToken()).resolves.toBeNull();
    expect(client.getSnapshot().status).toBe("anonymous");
    const destination = new URL(location.assign.mock.calls[0]?.[0] ?? "");
    expect(destination.origin + destination.pathname).toBe(`${ISSUER}/logout`);
    expect(destination.searchParams.get("id_token_hint")).toBe("id-token-hint");
    expect(destination.searchParams.get("post_logout_redirect_uri")).toBe(
      "https://app.example.com/login",
    );
  });

  it("rejects external and recursive login return destinations", () => {
    expect(sanitizeReturnTo("https://attacker.example/collect")).toBe("/select-organization");
    expect(sanitizeReturnTo("//attacker.example/collect")).toBe("/select-organization");
    expect(sanitizeReturnTo("/login?returnTo=/login")).toBe("/select-organization");
    expect(sanitizeReturnTo("/app/northstar/documents")).toBe("/app/northstar/documents");
  });
});

function HttpResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
