import "@testing-library/jest-dom/vitest";

import { server } from "./msw";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
