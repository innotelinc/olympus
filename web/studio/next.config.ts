import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Standalone output is for the container image only: it is what lets the
  // runtime image ship without a Node toolchain. `next start` (what
  // `make studio` runs) does NOT support standalone and warns on every boot,
  // so it stays off unless the Dockerfile asks for it via STUDIO_STANDALONE=1.
  output: process.env.STUDIO_STANDALONE === "1" ? "standalone" : undefined,
  poweredByHeader: false,
};

export default nextConfig;
