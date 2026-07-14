import type { NextConfig } from "next";

// ── Content-Security-Policy ──────────────────────────────────────────────────
// Derived so that `connect-src` matches the configured backend (same env var
// api.ts reads) and allows the Firebase/Google Auth origins the app relies on.
// `'unsafe-inline'` scripts are required by Next.js hydration; `'unsafe-eval'`
// is only enabled in dev (Next.js dev tooling needs it).
const PROD_FALLBACK_API_BASE = "https://rockstar00-hirelog-backend.hf.space";
const DEV_FALLBACK_API_BASE = "http://localhost:8000";
const isDev = process.env.NODE_ENV !== "production";

// connect-src must allow every base api.ts may fail over to, or a backend
// hiccup that trips failover becomes a hard CSP-blocked client error. Mirror
// api.ts's candidate set (env base + prod fallback, plus dev fallback in dev).
const apiConnectSrc = [
  process.env.NEXT_PUBLIC_API_BASE_URL,
  PROD_FALLBACK_API_BASE,
  isDev ? DEV_FALLBACK_API_BASE : null,
]
  .filter((b): b is string => Boolean(b))
  .map((b) => b.replace(/\/+$/, ""));

const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""} https://apis.google.com https://accounts.google.com`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  [
    "connect-src 'self'",
    ...apiConnectSrc,
    "https://*.googleapis.com",
    "https://*.firebaseio.com",
    "https://identitytoolkit.googleapis.com",
    "https://securetoken.googleapis.com",
    "https://*.firebaseapp.com",
  ].join(" "),
  "frame-src 'self' https://*.firebaseapp.com https://accounts.google.com https://apis.google.com",
  "form-action 'self'",
].join("; ");

const nextConfig: NextConfig = {
  // Strip console.log in production builds
  compiler: {
    removeConsole: process.env.NODE_ENV === "production" ? { exclude: ["error", "warn"] } : false,
  },

  // Security & caching headers
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
        ],
      },
      {
        // Cache static assets aggressively
        source: "/_next/static/(.*)",
        headers: [
          {
            key: "Cache-Control",
            value: "public, max-age=31536000, immutable",
          },
        ],
      },
    ];
  },

  // Redirect www → apex (optional — Vercel handles this too)
  async redirects() {
    return [];
  },
};

export default nextConfig;
