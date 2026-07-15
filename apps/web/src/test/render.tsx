import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render } from "@testing-library/react";

import { createQueryClient } from "../app/queryClient";
import { createAppRouter } from "../app/router";
import { AuthProvider } from "../auth/AuthContext";

/** Render the real app router at a path with a fresh query client. */
export async function renderApp(
  initialPath: string,
  options: { authStatus?: "test" | "anonymous" | "expired" | "configuration-error" } = {},
) {
  // The app's real defaults (staleTime, focus behavior), minus retries so
  // failure tests are deterministic.
  const queryClient = createQueryClient();
  queryClient.setDefaultOptions({
    queries: { ...queryClient.getDefaultOptions().queries, retry: false },
  });
  const router = createAppRouter(createMemoryHistory({ initialEntries: [initialPath] }));
  await router.load();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider testStatus={options.authStatus ?? "test"}>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { router, queryClient, ...utils };
}
