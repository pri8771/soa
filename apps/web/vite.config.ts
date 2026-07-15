/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * Production security headers for the web app (SEC-002). Served by
 * `vite preview` so the Playwright e2e suite exercises the REAL policy
 * against the built app; the production static host must serve the
 * same set. The CSP has NO unsafe script exceptions: script-src is
 * 'self' only. The narrow, documented allowances are style-src
 * 'unsafe-inline' (React style attributes — styles, not scripts) and
 * img-src blob:/data:/https: so the document viewer can render page
 * images fetched via short-lived signed storage URLs whose origin is
 * deployment-specific.
 */
export const securityHeaders = {
  "Content-Security-Policy": [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' blob: data: https:",
    "connect-src 'self' https: http://localhost:8000 http://127.0.0.1:8000",
    // Firebase Auth initializes a hidden helper iframe on authDomain. A
    // custom Firebase Hosting authDomain is same-origin; these two bounded
    // fallbacks cover the standard Firebase domains without allowing
    // arbitrary framing.
    "frame-src 'self' https://*.firebaseapp.com https://*.web.app",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; "),
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  // Firebase popup auth needs the opener relationship long enough to
  // complete its handshake; this still isolates unrelated top-level sites.
  "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
  "Cross-Origin-Resource-Policy": "same-origin",
};

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          // The Firebase SDK is needed only by the selected Firebase auth
          // mode. Keep its dynamically imported graph out of the initial
          // provider-neutral bundle so OIDC and public pages do not pay the
          // download/parse cost.
          if (id.includes("/node_modules/firebase/") || id.includes("/node_modules/@firebase/")) {
            return "vendor-firebase";
          }
          if (id.includes("@tanstack")) return "vendor-tanstack";
          if (
            id.includes("/node_modules/react/") ||
            id.includes("/node_modules/react-dom/") ||
            id.includes("/node_modules/scheduler/")
          )
            return "vendor-react";
          return "vendor";
        },
      },
    },
  },
  server: {
    port: 5173,
  },
  preview: {
    headers: securityHeaders,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    exclude: ["e2e/**", "node_modules/**"],
    // Full-router jsdom suites are memory/CPU heavy. Vitest otherwise uses
    // every reported core (18 on the release-gate host), starving async
    // renders until their normal 1s/5s assertions expire.
    maxWorkers: 4,
  },
});
