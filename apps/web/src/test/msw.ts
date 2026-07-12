/** Deterministic API mocks (MSW) for web tests. */

import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";

import { DEFAULT_ME } from "./me-payload";

export { DEFAULT_ME };

export const handlers = [http.get("/api/me", () => HttpResponse.json(DEFAULT_ME))];

export const server = setupServer(...handlers);
