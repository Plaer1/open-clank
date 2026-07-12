import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pure static export -> writes ./out, served by app.py (no Node server at runtime).
  output: "export",
  // Single-page app served at site root by app.py; clean directory URLs.
  trailingSlash: true,
  // No server = no Next image optimizer.
  images: { unoptimized: true },
  typescript: {
    ignoreBuildErrors: true,
  },
  reactStrictMode: false,
  // Lock Turbopack's root to THIS project dir. Without it Next infers the
  // workspace root from lockfiles and picks the parent dir (/home/e) because a
  // stray /home/e/package-lock.json exists — which makes Turbopack scan/build
  // from the wrong root. __dirname is always this next.config.ts's directory.
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
