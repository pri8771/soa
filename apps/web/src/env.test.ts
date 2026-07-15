import { parseEnv } from "./env";

describe("parseEnv", () => {
  it("applies defaults for optional values", () => {
    const env = parseEnv({ MODE: "test" });
    expect(env.VITE_API_BASE_URL).toBe("/api");
    expect(env.VITE_OIDC_SCOPE).toBe("openid profile email offline_access");
    expect(env.VITE_OIDC_BEARER_TOKEN).toBe("id_token");
  });

  it("keeps explicit values", () => {
    const env = parseEnv({ MODE: "test", VITE_API_BASE_URL: "https://api.example.com" });
    expect(env.VITE_API_BASE_URL).toBe("https://api.example.com");
  });

  it("throws a readable error for invalid configuration", () => {
    expect(() => parseEnv({ MODE: "test", VITE_API_BASE_URL: 42 })).toThrow(
      /Invalid environment configuration/,
    );
  });

  it("requires the OIDC issuer and client ID as a pair", () => {
    expect(() =>
      parseEnv({ MODE: "production", VITE_OIDC_ISSUER: "https://identity.example.com" }),
    ).toThrow(/OIDC issuer and client ID must be configured together/);
  });

  it("accepts a provider-neutral OIDC browser configuration", () => {
    const env = parseEnv({
      MODE: "production",
      VITE_AUTH_MODE: "oidc",
      VITE_OIDC_ISSUER: "https://identity.example.com",
      VITE_OIDC_CLIENT_ID: "soa-web",
      VITE_OIDC_AUDIENCE: "soa-api",
      VITE_OIDC_BEARER_TOKEN: "access_token",
    });
    expect(env.VITE_OIDC_CLIENT_ID).toBe("soa-web");
    expect(env.VITE_OIDC_AUDIENCE).toBe("soa-api");
    expect(env.VITE_OIDC_BEARER_TOKEN).toBe("access_token");
  });

  it("rejects an insecure production OIDC issuer", () => {
    expect(() =>
      parseEnv({
        MODE: "production",
        VITE_AUTH_MODE: "oidc",
        VITE_OIDC_ISSUER: "http://identity.example.com",
        VITE_OIDC_CLIENT_ID: "soa-web",
      }),
    ).toThrow(/production OIDC issuer must use HTTPS/);
  });

  it("requires an explicit, complete Firebase contract in production", () => {
    expect(() => parseEnv({ MODE: "production", VITE_AUTH_MODE: "firebase" })).toThrow(
      /VITE_FIREBASE_API_KEY/,
    );
  });

  it("accepts the reference Firebase Identity Platform configuration", () => {
    const env = parseEnv({
      MODE: "production",
      VITE_AUTH_MODE: "firebase",
      VITE_API_BASE_URL: "https://api.example.com",
      VITE_FIREBASE_API_KEY: "public-browser-key",
      VITE_FIREBASE_AUTH_DOMAIN: "soa-prod.firebaseapp.com",
      VITE_FIREBASE_PROJECT_ID: "soa-prod",
      VITE_FIREBASE_APP_ID: "1:123:web:abc",
      VITE_FIREBASE_PROVIDER_ID: "oidc.enterprise",
    });
    expect(env.VITE_AUTH_MODE).toBe("firebase");
    expect(env.VITE_FIREBASE_LOGIN_METHOD).toBe("redirect");
  });

  it("rejects a Firebase auth URL where the SDK requires a hostname", () => {
    expect(() =>
      parseEnv({
        MODE: "production",
        VITE_AUTH_MODE: "firebase",
        VITE_FIREBASE_API_KEY: "public-browser-key",
        VITE_FIREBASE_AUTH_DOMAIN: "https://soa-prod.firebaseapp.com/auth",
        VITE_FIREBASE_PROJECT_ID: "soa-prod",
        VITE_FIREBASE_APP_ID: "1:123:web:abc",
        VITE_FIREBASE_PROVIDER_ID: "oidc.enterprise",
      }),
    ).toThrow(/Firebase auth domain must be a hostname/);
  });
});
