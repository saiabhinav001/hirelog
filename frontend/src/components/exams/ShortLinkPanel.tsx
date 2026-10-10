"use client";

import { useCallback, useEffect, useState } from "react";

import { ConfirmDialog } from "@/components/exams/ConfirmDialog";
import { describeConflict } from "@/lib/examConflicts";
import { examEndpoints, type ShortLinkState } from "@/lib/examEndpoints";

type Props = {
  runId: string;
  getToken: () => Promise<string | undefined>;
};

/**
 * Join link for a run. The code is only a locator: students still sign in and must be enrolled, and admission,
 * content and submission are checked separately. Regenerating or revoking stops the old code at once.
 */
export function ShortLinkPanel({ runId, getToken }: Props) {
  const [state, setState] = useState<ShortLinkState | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"regenerate" | "revoke" | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await examEndpoints.getShortLink(await getToken(), runId));
    } catch (err) {
      setMessage({ kind: "error", text: describeConflict(err, "Could not load the join link.") });
    }
  }, [getToken, runId]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      await load();
      setMessage({ kind: "ok", text: done });
    } catch (err) {
      setMessage({ kind: "error", text: describeConflict(err) });
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const link = state?.link ?? null;
  const url = link && typeof window !== "undefined" ? `${window.location.origin}/exams/join/${link.code}` : null;

  const copy = async () => {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setMessage({ kind: "ok", text: "Link copied." });
    } catch {
      setMessage({ kind: "error", text: "Could not copy automatically. Select the link and copy it." });
    }
  };

  const share = async () => {
    if (!url || typeof navigator.share !== "function") return;
    try {
      await navigator.share({ title: "Join the exam", url });
    } catch {
      // cancelled by the user
    }
  };

  if (state && !state.enabled) {
    return (
      <section className="card mt-4 p-5" aria-labelledby="short-link-title">
        <h2 id="short-link-title" className="text-lg font-semibold">Join link</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">Short links are not enabled on this deployment.</p>
      </section>
    );
  }

  return (
    <section className="card mt-4 p-5" aria-labelledby="short-link-title">
      <h2 id="short-link-title" className="text-lg font-semibold">Join link</h2>
      <p className="mt-1 text-sm text-[var(--text-muted)]">
        A short link students can open to find this exam. It only locates the exam: students still sign in, must be enrolled, and are admitted by you.
      </p>

      {!state && <p className="mt-3 text-sm text-[var(--text-muted)]">Loading…</p>}

      {state && !link && (
        <button className="btn-primary mt-3" disabled={busy} onClick={() => act(() => getToken().then((t) => examEndpoints.issueShortLink(t, runId)), "Join link created.")}>
          Create join link
        </button>
      )}

      {link && (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <input className="input-field flex-1 font-mono" readOnly aria-label="Join link" value={url ?? ""} onFocus={(e) => e.currentTarget.select()} />
            <span className="badge font-mono" aria-label="Code">{link.display_code}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            <button className="btn-secondary" onClick={copy}>Copy link</button>
            {typeof navigator !== "undefined" && typeof navigator.share === "function" && <button className="btn-secondary" onClick={share}>Share</button>}
            <button className="btn-secondary" disabled={busy} onClick={() => setConfirm("regenerate")}>Regenerate</button>
            <button className="btn-secondary" disabled={busy} onClick={() => setConfirm("revoke")}>Revoke</button>
          </div>
        </div>
      )}

      <p role={message?.kind === "error" ? "alert" : "status"} className={`mt-3 text-sm ${message?.kind === "error" ? "text-[var(--error)]" : ""}`}>{message?.text}</p>

      {confirm === "regenerate" && (
        <ConfirmDialog
          title="Replace the join link?"
          consequence="The current link and code stop working immediately. Anyone holding the old link will need the new one."
          confirmLabel="Replace link"
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => act(() => getToken().then((t) => examEndpoints.issueShortLink(t, runId, true)), "New join link created. The old one no longer works.")}
        />
      )}
      {confirm === "revoke" && (
        <ConfirmDialog
          title="Revoke the join link?"
          consequence="The link stops working immediately. Students already admitted are not affected."
          confirmLabel="Revoke link"
          danger
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => act(() => getToken().then((t) => examEndpoints.revokeShortLink(t, runId)), "Join link revoked.")}
        />
      )}
    </section>
  );
}
