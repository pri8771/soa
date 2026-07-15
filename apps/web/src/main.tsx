import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@soa/design-system/tokens.css";
import "@soa/design-system/primitives.css";

import { getDevUser, setDevUser } from "./api/client";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { createQueryClient } from "./app/queryClient";
import { createAppRouter } from "./app/router";
import { AuthProvider } from "./auth/AuthContext";
import { env } from "./env";

// Local development convenience: with no production identity provider yet,
// default the dev-identity session to the seeded demo admin so a fresh
// `make seed` + open lands you signed in. Never runs in a production build.
if (env.MODE === "development" && env.VITE_AUTH_MODE === undefined && getDevUser() === null) {
  setDevUser("admin@northstar.example");
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

const queryClient = createQueryClient();
const router = createAppRouter();

createRoot(rootElement).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <RouterProvider router={router} />
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
