import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV !== "production";

/** The API origin the browser calls directly (none when it goes through the same-origin
 * /api proxy, as in the container). Mirrors the default in src/lib/config.ts. */
function apiOrigin(): string {
  const url = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";
  try {
    return /^https?:\/\//.test(url) ? new URL(url).origin : "";
  } catch {
    return "";
  }
}

/** Content-Security-Policy (PR-042). Next.js bootstraps with inline scripts, so script-src
 * keeps 'unsafe-inline' (no nonces without a middleware); everything else is locked to the
 * app's own origin, and nothing may frame the UI. Dev mode adds eval + HMR websockets. */
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  ["connect-src 'self'", apiOrigin(), isDev ? "ws: wss:" : ""].filter(Boolean).join(" "),
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  // The container (Dockerfile) sets NEXT_OUTPUT=standalone: a self-contained server.js with only
  // the needed node_modules. Local `next start` (e2e) keeps the default output.
  ...(process.env.NEXT_OUTPUT === "standalone" ? { output: "standalone" as const } : {}),
  reactStrictMode: true,
  poweredByHeader: false,
  typedRoutes: true,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
