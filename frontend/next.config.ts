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

// The exam API is a separate, independently deployed origin (docs/adr/0008) —
// pinned to exactly one base, unlike the legacy multi-base failover above.
// The live-control WebSocket (docs/adr/0012) reuses this SAME origin with the
// scheme swapped (http->ws, https->wss) — no separate env var, keeping the
// "pinned to one configured origin" contract intact for both REST and WS.
const examsApiHttpBases = [
  process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL,
  isDev ? "http://localhost:8020" : null,
]
  .filter((b): b is string => Boolean(b))
  .map((b) => b.replace(/\/+$/, ""));
const examsApiConnectSrc = [
  ...examsApiHttpBases,
  ...examsApiHttpBases.map((b) => b.replace(/^http/, "ws")),
];

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
    ...examsApiConnectSrc,
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
  // Pin the project root — a stray lockfile in a parent folder (outside this
  // repo) otherwise makes Turbopack misdetect the workspace root and fail to
  // resolve `frontend/node_modules` deps (e.g. tailwindcss), breaking CSS.
  turbopack: {
    root: __dirname,
  },

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
        // Exam routes need camera access for proctoring evidence capture
        // (ADR 0013's `useCameraCapture`) — the site-wide deny above would
        // otherwise silently block every `getUserMedia` call on these
        // pages (a real bug found while auditing for Prompt 10: student
        // camera captures would always fail closed as "permission
        // denied", with nothing to indicate the cause was this header,
        // not the student). Microphone/geolocation remain denied — the
        // exam module uses neither.
        source: "/exams/:path*",
        headers: [
          {
            key: "Permissions-Policy",
            value: "camera=(self), microphone=(), geolocation=()",
          },
          // Authenticated exam pages (questions, answers, results, evidence,
          // tokens) must never be stored by the browser, the back/forward
          // cache's disk layer, a shared proxy or a CDN. `private, no-store`
          // is the framework/CDN half of the exam PWA contract; the service
          // worker never caches these routes and the exam API sends no-store
          // on every response.
          { key: "Cache-Control", value: "private, no-store" },
        ],
      },
      // Cache static assets aggressively — PRODUCTION ONLY. Turbopack dev
      // chunk filenames are not reliably content-hashed per edit, so an
      // `immutable, max-age=31536000` header in dev makes the browser
      // permanently cache the first-loaded JS chunk and never revalidate
      // it again — every subsequent code change silently fails to show up
      // in the browser (confirmed live: correct source, clean compiles,
      // stale DOM, surviving even a full `.next` wipe + dev-server
      // restart, until this header was excluded in dev and the browser
      // cache was bypassed). Next.js's own startup warning ("Setting a
      // custom Cache-Control header can break Next.js development
      // behavior") was pointing at exactly this.
      ...(isDev
        ? []
        : [
            {
              source: "/_next/static/(.*)",
              headers: [
                {
                  key: "Cache-Control",
                  value: "public, max-age=31536000, immutable",
                },
              ],
            },
          ]),
    ];
  },

  // Redirect www → apex (optional — Vercel handles this too)
  async redirects() {
    return [];
  },
};

export default nextConfig;
