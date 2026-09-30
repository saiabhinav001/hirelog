"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type LockdownAdmissionLogEntry, type LockdownProfile } from "@/lib/examEndpoints";

function parseList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function LockdownSettingsContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [profile, setProfile] = useState<LockdownProfile | null>(null);
  const [log, setLog] = useState<LockdownAdmissionLogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [enabled, setEnabled] = useState(false);
  const [configKeyHash, setConfigKeyHash] = useState("");
  const [bekHashes, setBekHashes] = useState("");
  const [platforms, setPlatforms] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const [profileData, logData] = await Promise.all([
        examEndpoints.getLockdownProfile(token, runId),
        examEndpoints.listLockdownAdmissionLog(token, runId),
      ]);
      setProfile(profileData);
      setLog(logData.results);
      setEnabled(profileData.enabled);
      setConfigKeyHash(profileData.expected_config_key_hash ?? "");
      setBekHashes(profileData.expected_browser_exam_key_hashes.join(", "));
      setPlatforms(profileData.piloted_platforms.join(", "));
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the lockdown profile.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    load();
  }, [load]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const token = await getToken();
      await examEndpoints.configureLockdownProfile(token, runId, {
        enabled,
        expected_config_key_hash: configKeyHash.trim() || null,
        expected_browser_exam_key_hashes: parseList(bekHashes),
        piloted_platforms: parseList(platforms),
      });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the lockdown profile.");
    } finally {
      setSaving(false);
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!loaded || !profile) return <LoadingState message="Loading managed-lockdown settings…" />;

  return (
    <div className="page-container max-w-2xl py-10">
      <h1 className="text-2xl font-semibold">Managed lockdown (Safe Exam Browser)</h1>
      <p className="mt-2 text-sm text-[var(--text-muted)]">
        Strict mode denies ordinary browsers — installing the exam PWA alone never passes. Verification uses only the
        Config Key / Browser Exam Key HTTP headers SEB sends, never the User-Agent string. Copy these key hashes
        directly from SEB&apos;s own Config Tool / Preferences &quot;Exam&quot; tab after configuring a real SEB
        settings file — this system does not generate them for you.
      </p>
      <p className="mt-2 text-xs text-[var(--warning)]">
        Not yet validated against a real SEB client/device in this environment — pilot with real hardware before
        relying on this for a real exam (see the ADR).
      </p>

      <div className="card mt-6 p-6">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span className="label mb-0">Require managed lockdown for this run</span>
        </label>

        <div className="mt-4">
          <label htmlFor="config-key-hash" className="label">Config Key hash</label>
          <input
            id="config-key-hash"
            className="input-field w-full font-mono text-xs"
            value={configKeyHash}
            onChange={(e) => setConfigKeyHash(e.target.value)}
            placeholder="Paste the Config Key from SEB's Exam tab"
          />
        </div>

        <div className="mt-4">
          <label htmlFor="bek-hashes" className="label">Browser Exam Key hashes (comma-separated, one per SEB version/platform)</label>
          <input
            id="bek-hashes"
            className="input-field w-full font-mono text-xs"
            value={bekHashes}
            onChange={(e) => setBekHashes(e.target.value)}
          />
        </div>

        <div className="mt-4">
          <label htmlFor="piloted-platforms" className="label">Piloted platforms (attested by you, comma-separated)</label>
          <input
            id="piloted-platforms"
            className="input-field w-full text-xs"
            value={platforms}
            onChange={(e) => setPlatforms(e.target.value)}
            placeholder="e.g. SEB_Windows_3.6, SEB_macOS_3.6"
          />
        </div>

        <button className="btn-primary mt-4" disabled={saving} onClick={handleSave}>
          {saving ? "Saving…" : "Save lockdown settings"}
        </button>

        {enabled && !configKeyHash && parseList(bekHashes).length === 0 && (
          <p className="mt-2 text-xs text-[var(--error)]">
            Enabled with no key configured — every student admission will be denied (fails closed) until at least one
            key is saved.
          </p>
        )}
      </div>

      <div className="card mt-6 overflow-x-auto p-6">
        <h2 className="text-lg font-semibold">Admission log</h2>
        <table className="mt-3 w-full text-left text-sm">
          <thead>
            <tr className="text-[var(--text-muted)]">
              <th className="py-1 pr-3">UID</th>
              <th className="py-1 pr-3">Outcome</th>
              <th className="py-1 pr-3">At</th>
            </tr>
          </thead>
          <tbody>
            {log.map((entry) => (
              <tr key={entry.id} className="border-t border-[var(--border)]">
                <td className="py-1 pr-3 font-mono text-xs">{entry.uid}</td>
                <td className="py-1 pr-3">
                  <span className={`badge ${entry.outcome === "admitted" ? "badge-success" : "badge-error"}`}>
                    {entry.outcome.replace(/_/g, " ")}
                  </span>
                </td>
                <td className="py-1 pr-3 text-xs text-[var(--text-muted)]">{new Date(entry.at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {log.length === 0 && <p className="mt-2 text-sm text-[var(--text-muted)]">No admission attempts recorded yet.</p>}
      </div>
    </div>
  );
}

export default function LockdownSettingsPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <LockdownSettingsContent />
    </ExamProtectedRoute>
  );
}
