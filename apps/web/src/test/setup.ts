import "@testing-library/jest-dom/vitest";

import { server } from "./msw";

// jsdom deliberately leaves scrolling unimplemented. TanStack Router calls
// it after navigation, so provide the browser contract instead of flooding
// otherwise-green test output with irrelevant not-implemented traces.
window.scrollTo = vi.fn();

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
