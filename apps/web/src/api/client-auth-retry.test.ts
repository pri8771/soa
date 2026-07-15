import { HttpResponse, http } from "msw";

import { server } from "../test/msw";

const auth = vi.hoisted(() => ({
  getBearerToken: vi.fn<() => Promise<string | null>>(),
  forceRefreshBearerToken: vi.fn<() => Promise<string>>(),
  expireAuthSession: vi.fn(),
}));

vi.mock("../auth/runtime", () => auth);

import { apiFetch } from "./client";

describe("apiFetch bearer refresh", () => {
  beforeEach(() => {
    auth.getBearerToken.mockResolvedValue("old-id-token");
    auth.forceRefreshBearerToken.mockResolvedValue("replacement-id-token");
  });

  afterEach(() => vi.clearAllMocks());

  it("refreshes once after a 401 and retries with the replacement token", async () => {
    const authorizations: Array<string | null> = [];
    server.use(
      http.get("/api/auth-retry-test", ({ request }) => {
        authorizations.push(request.headers.get("Authorization"));
        if (authorizations.length === 1) {
          return HttpResponse.json(
            { error: { code: "unauthorized", message: "Expired.", correlation_id: "corr-1" } },
            { status: 401 },
          );
        }
        return HttpResponse.json({ ok: true });
      }),
    );

    await expect(apiFetch("/auth-retry-test")).resolves.toEqual({ ok: true });
    expect(authorizations).toEqual(["Bearer old-id-token", "Bearer replacement-id-token"]);
    expect(auth.forceRefreshBearerToken).toHaveBeenCalledTimes(1);
    expect(auth.expireAuthSession).not.toHaveBeenCalled();
  });
});
