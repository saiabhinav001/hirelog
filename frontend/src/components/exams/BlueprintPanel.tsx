"use client";

import { useCallback, useEffect, useState } from "react";

import {
  examEndpoints,
  type ApprovedSourceSummary,
  type BlueprintCapacity,
  type GenerationCandidate,
  type PublicationPreview,
} from "@/lib/examEndpoints";

type Props = {
  definitionId: string;
  blueprint: { id: string; title: string };
  sources: ApprovedSourceSummary[];
  getToken: () => Promise<string | undefined>;
  onChanged?: () => void | Promise<void>;
};

const STATUS_LABEL: Record<string, string> = {
  ready_for_human_review: "needs your review",
  needs_revision: "needs revision",
  approved: "approved",
  published: "published",
  rejected: "rejected",
  superseded: "replaced by an edit",
};

/**
 * Per-blueprint generation workspace (Prompt 5): requested / generated / valid / approved counts,
 * generation from approved sources, teacher review + edit, the explicit shortfall acceptance, and
 * publishing with the server's blockers shown. Approval and publishing are always teacher actions.
 */
export function BlueprintPanel({ definitionId, blueprint, sources, getToken, onChanged }: Props) {
  const [capacity, setCapacity] = useState<BlueprintCapacity | null>(null);
  const [candidates, setCandidates] = useState<GenerationCandidate[]>([]);
  const [preview, setPreview] = useState<PublicationPreview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [shortfallReason, setShortfallReason] = useState("");

  const refresh = useCallback(async () => {
    const token = await getToken();
    const [cap, list, prev] = await Promise.all([
      examEndpoints.getBlueprintCapacity(token, blueprint.id),
      examEndpoints.listCandidatesForBlueprint(token, blueprint.id),
      examEndpoints.getPublishPreview(token, definitionId, blueprint.id),
    ]);
    setCapacity(cap);
    setCandidates(list.results.filter((c) => c.status !== "superseded"));
    setPreview(prev);
  }, [blueprint.id, definitionId, getToken]);

  useEffect(() => {
    refresh().catch((err) => setMessage(err instanceof Error ? err.message : "Could not load this blueprint."));
  }, [refresh]);

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label);
    setMessage(null);
    try {
      await action();
      await refresh();
      await onChanged?.();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "That did not work.");
    } finally {
      setBusy(null);
    }
  };

  const approvedSourceIds = sources.filter((s) => s.review_status === "approved").map((s) => s.id);

  const generate = () =>
    run("generate", async () => {
      if (approvedSourceIds.length === 0) throw new Error("Approve at least one source before generating questions.");
      const token = await getToken();
      const result = await examEndpoints.generateForBlueprint(token, blueprint.id, approvedSourceIds);
      const failed = result.results.filter((r) => r.status !== "succeeded");
      const withheld = result.results.reduce((n, r) => n + r.excluded_anchors.length, 0);
      const notes = [
        failed.length ? `${failed.length} slot(s) did not generate: ${failed[0].error ?? failed[0].status}.` : "",
        withheld ? `${withheld} passage(s) were withheld because they read like instructions.` : "",
        result.remaining_slots.length ? `${result.remaining_slots.length} slot(s) remain - generate again to continue.` : "",
      ].filter(Boolean);
      if (notes.length) setMessage(notes.join(" "));
    });

  const review = (id: string, decision: "approve" | "reject") =>
    run(`review-${id}`, async () => {
      await examEndpoints.reviewCandidate(await getToken(), id, decision, decision === "reject" ? "Rejected by teacher." : undefined);
    });

  const saveEdit = () =>
    run("edit", async () => {
      if (!editing) return;
      const result = await examEndpoints.editCandidate(await getToken(), editing.id, { public_prompt: editing.text });
      setEditing(null);
      if (result.kind === "new_version") setMessage("Saved as a new version. It needs to be approved again.");
    });

  const acceptShortfall = (accepted: boolean) =>
    run("shortfall", async () => {
      await examEndpoints.setBlueprintShortfall(await getToken(), blueprint.id, accepted, shortfallReason.trim() || undefined);
    });

  const publish = () =>
    run("publish", async () => {
      const revision = await examEndpoints.publishBlueprint(await getToken(), definitionId, blueprint.id);
      setMessage(`Published as revision ${revision.revision_number}.`);
    });

  const totals = capacity?.totals;
  const blocked = preview && !preview.publishable;

  return (
    <div className="rounded-md border border-[var(--border)] p-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">{blueprint.title}</h3>
        <button className="btn-secondary" disabled={busy !== null} onClick={generate}>
          {busy === "generate" ? "Generating…" : "Generate questions"}
        </button>
      </div>

      {totals && (
        <p className="mt-2 text-sm text-[var(--text-muted)]" data-testid="capacity-summary">
          Requested {totals.requested} · generated {totals.generated} · valid {totals.valid} · approved {totals.approved}
          {totals.pending_review > 0 && ` · ${totals.pending_review} awaiting your review`}
        </p>
      )}

      {message && (
        <p role="status" className="mt-2 text-sm">
          {message}
        </p>
      )}

      <div className="mt-2 space-y-2">
        {candidates.map((c) => (
          <div key={c.id} className="rounded-md bg-[var(--surface-muted)] p-2 text-sm">
            <div className="flex items-center justify-between gap-2">
              <div>
                <span className="badge mr-2">{c.item_type}</span>
                <span className="badge mr-2">{STATUS_LABEL[c.status] ?? c.status}</span>
                {(c.version ?? 1) > 1 && <span className="badge mr-2">v{c.version}</span>}
                <span className="text-[var(--text-muted)]">{c.public_prompt.slice(0, 80)}</span>
              </div>
              <div className="flex gap-2">
                {c.status === "ready_for_human_review" && (
                  <>
                    <button className="btn-secondary" disabled={busy !== null} onClick={() => review(c.id, "approve")}>Approve</button>
                    <button className="btn-secondary" disabled={busy !== null} onClick={() => review(c.id, "reject")}>Reject</button>
                  </>
                )}
                {["ready_for_human_review", "needs_revision", "approved"].includes(c.status) && (
                  <button className="btn-secondary" onClick={() => setEditing({ id: c.id, text: c.public_prompt })}>Edit</button>
                )}
              </div>
            </div>
            {c.status === "needs_revision" && (c.validation_issues ?? []).length > 0 && (
              <ul className="mt-1 list-disc pl-5 text-xs text-[var(--text-muted)]">
                {(c.validation_issues ?? []).filter((i) => i.severity !== "minor").slice(0, 3).map((i, n) => (
                  <li key={n}>{i.reason}</li>
                ))}
              </ul>
            )}
            {editing?.id === c.id && (
              <div className="mt-2 space-y-2">
                <textarea
                  className="input-field w-full"
                  aria-label="Question text"
                  rows={3}
                  value={editing.text}
                  onChange={(e) => setEditing({ id: c.id, text: e.target.value })}
                />
                <p className="text-xs text-[var(--text-muted)]">Changing the question creates a new version that must be approved again.</p>
                <div className="flex gap-2">
                  <button className="btn-secondary" disabled={busy !== null} onClick={saveEdit}>Save</button>
                  <button className="btn-secondary" onClick={() => setEditing(null)}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {candidates.length === 0 && <p className="text-xs text-[var(--text-muted)]">No questions generated for this blueprint yet.</p>}
      </div>

      {preview && (
        <div className="mt-3 border-t border-[var(--border)] pt-3 text-sm">
          {blocked && (
            <ul className="list-disc pl-5" aria-label="Publishing blockers">
              {preview.blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          )}
          {capacity && capacity.empty_slots.length === 0 && !capacity.meets_requirement && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {capacity.shortfall_accepted ? (
                <>
                  <span className="badge badge-warning">Shortfall accepted</span>
                  <button className="btn-secondary" disabled={busy !== null} onClick={() => acceptShortfall(false)}>Withdraw</button>
                </>
              ) : (
                <>
                  <input
                    className="input-field flex-1"
                    aria-label="Reason for publishing with fewer variants"
                    placeholder="Why is fewer variants acceptable?"
                    value={shortfallReason}
                    onChange={(e) => setShortfallReason(e.target.value)}
                  />
                  <button className="btn-secondary" disabled={busy !== null || !shortfallReason.trim()} onClick={() => acceptShortfall(true)}>
                    Accept shortfall
                  </button>
                </>
              )}
            </div>
          )}
          <div className="mt-2 flex items-center justify-between">
            <span className="text-[var(--text-muted)]">
              {preview.slot_count} question(s) · {preview.total_marks} marks · {preview.approved_variants_total}/{preview.requested_variants_total} variants approved
            </span>
            <button className="btn-primary" disabled={busy !== null || !preview.publishable} onClick={publish}>
              Publish this blueprint
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
