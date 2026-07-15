import { FirebaseAuthClient, type FirebaseBrowserConfig } from "./firebase";

const CONFIG: FirebaseBrowserConfig = {
  apiKey: "public-key",
  authDomain: "soa.firebaseapp.com",
  projectId: "soa-project",
  appId: "1:123:web:abc",
  tenantId: "tenant-a",
  providerId: "oidc.enterprise",
  loginMethod: "redirect",
};

function fakeSdk(options: { user?: ReturnType<typeof fakeUser> | null; popupError?: string } = {}) {
  const user = options.user === undefined ? fakeUser() : options.user;
  const auth = { currentUser: user, tenantId: null as string | null };
  const providerParameters: Record<string, string>[] = [];
  class Provider {
    constructor(public readonly providerId: string) {}
    setCustomParameters(parameters: Record<string, string>) {
      providerParameters.push(parameters);
    }
  }
  const sdk = {
    app: {
      getApps: vi.fn(() => []),
      initializeApp: vi.fn(() => ({ name: "soa-web" })),
    },
    auth: {
      getAuth: vi.fn(() => auth),
      browserSessionPersistence: { type: "SESSION" },
      setPersistence: vi.fn(async () => undefined),
      OAuthProvider: Provider,
      onIdTokenChanged: vi.fn((_auth: unknown, callback: (next: typeof user) => void) => {
        queueMicrotask(() => callback(auth.currentUser));
        return vi.fn();
      }),
      getRedirectResult: vi.fn(async () => null),
      signInWithRedirect: vi.fn(async () => undefined),
      signInWithPopup: vi.fn(async () => {
        if (options.popupError) throw { code: options.popupError };
        return { user };
      }),
      signOut: vi.fn(async () => {
        auth.currentUser = null;
      }),
    },
  };
  return { sdk, auth, user, providerParameters };
}

function fakeUser() {
  return {
    getIdToken: vi.fn(async (force: boolean) => (force ? "fresh-firebase-id" : "firebase-id")),
    getIdTokenResult: vi.fn(async () => ({
      expirationTime: new Date(Date.now() + 60 * 60 * 1_000).toISOString(),
    })),
  };
}

describe("FirebaseAuthClient", () => {
  beforeEach(() => sessionStorage.clear());

  it("uses session persistence and Firebase ID-token refresh", async () => {
    const fake = fakeSdk();
    const client = new FirebaseAuthClient(CONFIG, {
      loadSdk: async () => fake.sdk as never,
      storage: sessionStorage,
    });

    await client.bootstrap();

    expect(fake.sdk.auth.setPersistence).toHaveBeenCalledWith(
      fake.auth,
      fake.sdk.auth.browserSessionPersistence,
    );
    expect(fake.auth.tenantId).toBe("tenant-a");
    expect(client.getSnapshot().status).toBe("authenticated");
    await expect(client.getBearerToken()).resolves.toBe("firebase-id");
    await expect(client.forceRefreshBearerToken()).resolves.toBe("fresh-firebase-id");
    expect(fake.user?.getIdToken).toHaveBeenLastCalledWith(true);
  });

  it("falls back from a blocked popup to a redirect without losing returnTo", async () => {
    const fake = fakeSdk({ popupError: "auth/popup-blocked" });
    const client = new FirebaseAuthClient(
      { ...CONFIG, loginMethod: "popup" },
      { loadSdk: async () => fake.sdk as never, storage: sessionStorage },
    );

    await client.beginLogin("/app/northstar/settings");

    expect(fake.sdk.auth.signInWithPopup).toHaveBeenCalledTimes(1);
    expect(fake.sdk.auth.signInWithRedirect).toHaveBeenCalledTimes(1);
    expect(client.consumeReturnTo()).toBe("/app/northstar/settings");
    expect(fake.providerParameters).toContainEqual({ prompt: "select_account" });
  });

  it("signs out through Firebase without retaining a bearer", async () => {
    const fake = fakeSdk();
    const client = new FirebaseAuthClient(CONFIG, {
      loadSdk: async () => fake.sdk as never,
      storage: sessionStorage,
    });
    await client.bootstrap();

    await client.logout();

    expect(fake.sdk.auth.signOut).toHaveBeenCalledWith(fake.auth);
    expect(client.getSnapshot().status).toBe("anonymous");
    await expect(client.getBearerToken()).resolves.toBeNull();
  });
});
