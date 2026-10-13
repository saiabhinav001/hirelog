"use client";

/**
 * Authenticated WebSocket ticket admission + per-stream ordering/dedup +
 * state-version reconciliation + reconnect with jittered backoff + REST
 * fallback polling (contract: "Socket events never override server
 * guards" — every accepted event just triggers `onReconcile`, which the
 * caller uses to refetch real state over REST; the event payload itself is
 * advisory only, never applied directly as authoritative state).
 */

import { useEffect, useRef, useState } from "react";

import { examApiFetch } from "@/lib/examsApi";

export type ConnectionHealth = "connecting" | "connected" | "reconnecting" | "polling" | "offline";

type LiveEventMessage = {
  seq: number;
  kind: "snapshot" | "run_state_changed" | "attempt_status_changed" | "heartbeat";
  state_version: number;
  payload: Record<string, unknown>;
};

const BASE_RECONNECT_DELAY_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 30000;
const ATTEMPTS_BEFORE_POLLING_FALLBACK = 5;
const DEFAULT_POLL_INTERVAL_MS = 10000;
const POLLING_MODE_RECONNECT_DELAY_MS = 120000;

function wsUrlFromApiBase(apiBase: string): string {
  return apiBase.replace(/^http/, "ws").replace(/\/+$/, "");
}

function withJitter(base: number): number {
  const jitter = base * 0.3 * (Math.random() * 2 - 1);
  return Math.max(250, base + jitter);
}

function jitteredDelay(attempt: number): number {
  return withJitter(Math.min(BASE_RECONNECT_DELAY_MS * 2 ** attempt, MAX_RECONNECT_DELAY_MS));
}

export function useLiveSocket(options: {
  runId: string | null;
  getToken: () => Promise<string | undefined>;
  onReconcile: () => void;
  onPoll?: () => void;
  apiBase?: string;
  pollIntervalMs?: number;
}): { health: ConnectionHealth; lastEventAt: number | null } {
  const { runId, getToken, onReconcile, pollIntervalMs = DEFAULT_POLL_INTERVAL_MS } = options;
  const apiBase = options.apiBase ?? process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL ?? "http://localhost:8020";

  const [health, setHealth] = useState<ConnectionHealth>("connecting");
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const lastSeqRef = useRef<number>(-1);
  const attemptRef = useRef(0);
  const unmountedRef = useRef(false);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onReconcileRef = useRef(onReconcile);
  onReconcileRef.current = onReconcile;
  const onPollRef = useRef(options.onPoll);
  onPollRef.current = options.onPoll;

  useEffect(() => {
    unmountedRef.current = false;
    if (!runId) {
      return undefined;
    }

    const stopPolling = () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };

    const startPollingFallback = () => {
      if (pollTimerRef.current) return;
      setHealth("polling");
      pollTimerRef.current = setInterval(() => (onPollRef.current ?? onReconcileRef.current)(), pollIntervalMs);
    };

    const scheduleReconnect = () => {
      if (unmountedRef.current) return;
      attemptRef.current += 1;
      if (attemptRef.current >= ATTEMPTS_BEFORE_POLLING_FALLBACK) {
        startPollingFallback();
      } else {
        setHealth("reconnecting");
      }
      // Every try mints a ticket (a database write), so a socket that cannot connect backs off far longer.
      const delay = pollTimerRef.current
        ? withJitter(POLLING_MODE_RECONNECT_DELAY_MS)
        : jitteredDelay(Math.min(attemptRef.current, 6));
      reconnectTimerRef.current = setTimeout(connect, delay);
    };

    async function connect() {
      if (unmountedRef.current || !runId) return;
      try {
        const token = await getToken();
        const { ticket } = await examApiFetch<{ ticket: string }>(
          "/api/exams/v1/live/ticket",
          { method: "POST", body: JSON.stringify({ run_id: runId }) },
          token
        );
        if (unmountedRef.current) return;

        const wsUrl = `${wsUrlFromApiBase(apiBase)}/api/exams/v1/live/ws?ticket=${encodeURIComponent(ticket)}&run_id=${encodeURIComponent(runId)}`;
        const socket = new WebSocket(wsUrl);
        socketRef.current = socket;

        socket.onopen = () => {
          if (unmountedRef.current) return;
          attemptRef.current = 0;
          stopPolling();
          setHealth("connected");
        };

        socket.onmessage = (event: MessageEvent<string>) => {
          let parsed: LiveEventMessage;
          try {
            parsed = JSON.parse(event.data);
          } catch {
            return; // malformed frame — ignore, never crash the session over it
          }
          if (parsed.kind === "heartbeat") return;
          setLastEventAt(Date.now());

          if (parsed.kind === "snapshot") {
            lastSeqRef.current = parsed.seq;
            onReconcileRef.current();
            return;
          }
          if (parsed.seq <= lastSeqRef.current) {
            return; // duplicate/stale — already applied, dedupe silently
          }
          // A gap (seq jumped by more than 1) and an in-order event are
          // handled identically: reconcile over REST rather than trust the
          // payload. The dedupe value above is in NOT re-reconciling for
          // messages we've already seen.
          lastSeqRef.current = parsed.seq;
          onReconcileRef.current();
        };

        socket.onerror = () => {
          // onclose fires right after in browsers — let that path reconnect.
        };

        socket.onclose = () => {
          if (socketRef.current === socket) {
            socketRef.current = null;
          }
          if (!unmountedRef.current) {
            scheduleReconnect();
          }
        };
      } catch {
        if (!unmountedRef.current) {
          scheduleReconnect();
        }
      }
    }

    connect();

    return () => {
      unmountedRef.current = true;
      stopPolling();
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      socketRef.current?.close();
      socketRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, apiBase, pollIntervalMs]);

  return { health, lastEventAt };
}
