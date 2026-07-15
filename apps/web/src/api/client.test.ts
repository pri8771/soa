import { HttpResponse, http } from "msw";

import { getBearerToken, installAuthSessionForTests, resetAuthSessionForTests } from "../auth/oidc";
import { server } from "../test/msw";
import { ApiError, DEV_USER_STORAGE_KEY, apiFetch } from "./client";

describe("apiFetch authentication and error contract", () => {
  afterEach(() => {
    resetAuthSessionForTests();
    localStorage.clear();
  });

  it("injects a bearer token and strips development identity outside development", async () => {
    installAuthSessionForTests({ accessToken: "signed-browser-token" });
    localStorage.setItem(DEV_USER_STORAGE_KEY, "admin@northstar.example");
    let authorization: string | null = null;
    let devUser: string | null = null;
    server.use(
      http.get("/api/auth-client-test", ({ request }) => {
        authorization = request.headers.get("Authorization");
        devUser = request.headers.get("X-Dev-User");
        return HttpResponse.json({ ok: true });
      }),
    );

    await expect(apiFetch<{ ok: boolean }>("/auth-client-test")).resolves.toEqual({ ok: true });
    expect(authorization).toBe("Bearer signed-browser-token");
    expect(devUser).toBeNull();
  });

  it("retains backend code, correlation, validation, and retry metadata", async () => {
    server.use(
      http.post("/api/auth-client-test", () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_error",
              message: "Request validation failed.",
              correlation_id: "corr-body-1",
              details: [
                {
                  location: ["body", "quantity"],
                  message: "Input should be a valid integer",
                  type: "int_parsing",
                },
              ],
            },
          },
          { status: 422, headers: { "Retry-After": "3", "X-Request-ID": "corr-header" } },
        ),
      ),
    );

    const error = await apiFetch("/auth-client-test", {
      method: "POST",
      body: JSON.stringify({ quantity: "many" }),
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 422,
      code: "validation_error",
      correlationId: "corr-body-1",
      retryAfter: "3",
      retryAfterSeconds: 3,
      details: [
        {
          location: ["body", "quantity"],
          message: "Input should be a valid integer",
          type: "int_parsing",
        },
      ],
    });
  });

  it("expires the local session after an unauthorized bearer response", async () => {
    installAuthSessionForTests({ accessToken: "rejected-token" });
    server.use(
      http.get("/api/auth-client-test", () =>
        HttpResponse.json(
          {
            error: {
              code: "unauthorized",
              message: "Authentication required.",
              correlation_id: "corr-401",
            },
          },
          { status: 401 },
        ),
      ),
    );

    await expect(apiFetch("/auth-client-test")).rejects.toMatchObject({
      status: 401,
      code: "unauthorized",
    });
    await expect(getBearerToken()).resolves.toBeNull();
  });
});
