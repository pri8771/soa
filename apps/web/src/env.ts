import { z } from "zod";

const optionalString = z.preprocess(
  (value) => (value === "" ? undefined : value),
  z.string().trim().min(1).optional(),
);

const optionalUrl = z.preprocess(
  (value) => (value === "" ? undefined : value),
  z.string().url().optional(),
);

/**
 * Environment configuration is validated at startup so a misconfigured build
 * fails loudly instead of producing broken API calls at runtime.
 */
const EnvSchema = z
  .object({
    MODE: z.string(),
    VITE_API_BASE_URL: z.string().default("/api"),
    VITE_AUTH_MODE: z.enum(["firebase", "oidc"]).optional(),
    VITE_FIREBASE_API_KEY: optionalString,
    VITE_FIREBASE_AUTH_DOMAIN: optionalString,
    VITE_FIREBASE_PROJECT_ID: optionalString,
    VITE_FIREBASE_APP_ID: optionalString,
    VITE_FIREBASE_TENANT_ID: optionalString,
    VITE_FIREBASE_PROVIDER_ID: optionalString,
    VITE_FIREBASE_LOGIN_METHOD: z.enum(["redirect", "popup"]).default("redirect"),
    VITE_OIDC_ISSUER: optionalUrl,
    VITE_OIDC_CLIENT_ID: optionalString,
    VITE_OIDC_SCOPE: z.string().trim().min(1).default("openid profile email offline_access"),
    VITE_OIDC_AUDIENCE: optionalString,
    // Firebase/Identity Platform authenticates the API with its signed ID
    // token. Other providers may expose a JWT access token instead.
    VITE_OIDC_BEARER_TOKEN: z.enum(["id_token", "access_token"]).default("id_token"),
  })
  .superRefine((value, context) => {
    if (Boolean(value.VITE_OIDC_ISSUER) !== Boolean(value.VITE_OIDC_CLIENT_ID)) {
      context.addIssue({
        code: "custom",
        path: [value.VITE_OIDC_ISSUER ? "VITE_OIDC_CLIENT_ID" : "VITE_OIDC_ISSUER"],
        message: "OIDC issuer and client ID must be configured together",
      });
    }
    if (value.MODE !== "production") return;
    if (!value.VITE_AUTH_MODE) {
      context.addIssue({
        code: "custom",
        path: ["VITE_AUTH_MODE"],
        message: "production builds must explicitly select firebase or oidc authentication",
      });
      return;
    }
    if (value.VITE_API_BASE_URL !== "/api" && !value.VITE_API_BASE_URL.startsWith("https://")) {
      context.addIssue({
        code: "custom",
        path: ["VITE_API_BASE_URL"],
        message: "production API URL must use HTTPS or the same-origin /api proxy",
      });
    }
    if (value.VITE_AUTH_MODE === "oidc") {
      if (!value.VITE_OIDC_ISSUER || !value.VITE_OIDC_CLIENT_ID) {
        context.addIssue({
          code: "custom",
          path: ["VITE_OIDC_ISSUER"],
          message: "generic OIDC mode requires issuer and client ID",
        });
      }
      if (value.VITE_OIDC_ISSUER && new URL(value.VITE_OIDC_ISSUER).protocol !== "https:") {
        context.addIssue({
          code: "custom",
          path: ["VITE_OIDC_ISSUER"],
          message: "production OIDC issuer must use HTTPS",
        });
      }
      return;
    }
    const firebaseFields = [
      "VITE_FIREBASE_API_KEY",
      "VITE_FIREBASE_AUTH_DOMAIN",
      "VITE_FIREBASE_PROJECT_ID",
      "VITE_FIREBASE_APP_ID",
      "VITE_FIREBASE_PROVIDER_ID",
    ] as const;
    for (const field of firebaseFields) {
      if (!value[field]) {
        context.addIssue({
          code: "custom",
          path: [field],
          message: "Firebase authentication requires this build-time value",
        });
      }
    }
    if (
      value.VITE_FIREBASE_AUTH_DOMAIN &&
      !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(value.VITE_FIREBASE_AUTH_DOMAIN)
    ) {
      context.addIssue({
        code: "custom",
        path: ["VITE_FIREBASE_AUTH_DOMAIN"],
        message: "Firebase auth domain must be a hostname without a scheme or path",
      });
    }
  });

export type Env = z.infer<typeof EnvSchema>;

export function parseEnv(raw: Record<string, unknown>): Env {
  const result = EnvSchema.safeParse(raw);
  if (!result.success) {
    const issues = result.error.issues
      .map((issue) => `${issue.path.join(".")}: ${issue.message}`)
      .join("; ");
    throw new Error(`Invalid environment configuration — ${issues}`);
  }
  return result.data;
}

export const env: Env = parseEnv(import.meta.env);
