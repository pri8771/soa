import { parseEnv } from "./env";

describe("parseEnv", () => {
  it("applies defaults for optional values", () => {
    const env = parseEnv({ MODE: "test" });
    expect(env.VITE_API_BASE_URL).toBe("/api");
  });

  it("keeps explicit values", () => {
    const env = parseEnv({ MODE: "test", VITE_API_BASE_URL: "https://api.example.com" });
    expect(env.VITE_API_BASE_URL).toBe("https://api.example.com");
  });

  it("throws a readable error for invalid configuration", () => {
    expect(() => parseEnv({ MODE: "test", VITE_API_BASE_URL: 42 })).toThrow(
      /Invalid environment configuration/,
    );
  });
});
