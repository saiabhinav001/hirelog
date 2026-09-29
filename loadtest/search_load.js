// k6 load test for the HireLog search endpoint.
//
// Drives a ramping load against GET /api/search and asserts latency/error SLOs.
// The search path is the most expensive endpoint (hybrid semantic + lexical),
// so it is the right thing to size capacity against.
//
// Run:
//   BASE_URL=https://<host> TOKEN=<firebase_id_token> k6 run loadtest/search_load.js
//
// TOKEN must be a valid Firebase ID token (search requires auth). For a
// throwaway load environment, set E2E_AUTH_BYPASS on the backend and pass the
// bypass token your e2e setup uses.

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.TOKEN || "";

const searchLatency = new Trend("search_latency_ms", true);

const QUERIES = [
  "binary search tree",
  "system design url shortener",
  "dynamic programming knapsack",
  "sql joins indexing",
  "process vs thread mutex",
  "dijkstra shortest path",
  "acid transactions",
  "virtual memory paging",
];

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "30s", target: 20 }, // warm up
        { duration: "1m", target: 50 }, // sustained load
        { duration: "30s", target: 100 }, // spike
        { duration: "30s", target: 0 }, // ramp down
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: {
    // SLOs — the build/report fails if these are breached.
    http_req_failed: ["rate<0.01"], // < 1% errors
    "search_latency_ms": ["p(95)<1500", "p(99)<3000"],
  },
};

export default function () {
  const q = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  const url = `${BASE_URL}/api/search?q=${encodeURIComponent(q)}&limit=20`;
  const params = {
    headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
    tags: { name: "search" },
  };

  const res = http.get(url, params);
  searchLatency.add(res.timings.duration);

  check(res, {
    "status is 200 or 429": (r) => r.status === 200 || r.status === 429,
    "not a server error": (r) => r.status < 500,
  });

  sleep(Math.random() * 1 + 0.5); // 0.5–1.5s think time
}
