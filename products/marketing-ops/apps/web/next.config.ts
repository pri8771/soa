import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  transpilePackages: ["@marketing-ops/domain", "@marketing-ops/test-fixtures", "@marketing-ops/ui"],
};

export default nextConfig;
