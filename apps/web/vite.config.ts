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
};

export default defineConfig({
  plugins: [react()],
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
  },
});
