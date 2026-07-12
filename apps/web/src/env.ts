import { z } from "zod";

/**
 * Environment configuration is validated at startup so a misconfigured build
 * fails loudly instead of producing broken API calls at runtime.
 */
const EnvSchema = z.object({
  MODE: z.string(),
  VITE_API_BASE_URL: z.string().default("/api"),
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
