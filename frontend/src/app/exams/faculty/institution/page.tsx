"use client";

import { signOut } from "firebase/auth";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import {
  examEndpoints,
  type InstitutionList,
  type InstitutionMember,
  type RosterJob,
  type RosterRowInput,
} from "@/lib/examEndpoints";
import { ExamApiError } from "@/lib/examsApi";
import { auth } from "@/lib/firebase";
import { parseRoster } from "@/lib/rosterParse";

const APPLY_BATCH_SIZE = 200;

type Notice = { kind: "ok" | "error"; text: string };

function InstitutionAdmin() {
  const router = useRouter();
  const { getToken, refresh } = useExamAuth();

  const [institutions, setInstitutions] = useState<InstitutionList | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [members, setMembers] = useState<InstitutionMember[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [needsReauth, setNeedsReauth] = useState(false);
  const [busy, setBusy] = useState(false);

  const [newName, setNewName] = useState("");
  const [rosterText, setRosterText] = useState("");
  const [rosterRows, setRosterRows] = useState<RosterRowInput[]>([]);
  const [job, setJob] = useState<RosterJob | null>(null);

  const selected = institutions?.results.find((item) => item.id === selectedId) ?? null;

  const loadInstitutions = useCallback(
    async (preferId?: string) => {
      try {
        const token = await getToken();
        const data = await examEndpoints.listInstitutions(token);
        setInstitutions(data);
        setSelectedId((current) => preferId || current || data.results[0]?.id || "");
      } catch (err) {
        setLoadError(err instanceof Error ? err.message : "Could not load your institutions.");
      }
    },
    [getToken]
  );

  const loadMembers = useCallback(
    async (institutionId: string) => {
      try {
        const token = await getToken();
        const data = await examEndpoints.listInstitutionMembers(token, institutionId);
        setMembers(data.results);
      } catch (err) {
        setLoadError(err instanceof Error ? err.message : "Could not load the roster.");
      }
    },
    [getToken]
  );

  useEffect(() => {
    loadInstitutions();
  }, [loadInstitutions]);

  useEffect(() => {
    if (selectedId) {
      setMembers(null);
      setJob(null);
      loadMembers(selectedId);
    }
  }, [loadMembers, selectedId]);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof ExamApiError && err.code === "reauthentication_required") {
      setNeedsReauth(true);
    }
    setNotice({ kind: "error", text: err instanceof Error ? err.message : fallback });
  };

  const handleCreateInstitution = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!newName.trim()) return;
    setBusy(true);
    setNotice(null);
    try {
      const token = await getToken();
      const created = await examEndpoints.createInstitution(token, newName.trim());
      setNewName("");
      await loadInstitutions(created.id);
      setNotice({ kind: "ok", text: `Created ${created.name}.` });
    } catch (err) {
      fail(err, "Could not create the institution.");
    } finally {
      setBusy(false);
    }
  };

  const handleDryRun = async () => {
    const parsed = parseRoster(rosterText);
    setJob(null);
    setRosterRows(parsed.rows);
    if (parsed.problems.length > 0) {
      setNotice({ kind: "error", text: parsed.problems.slice(0, 5).join(" ") });
      return;
    }
    if (parsed.rows.length === 0) {
      setNotice({ kind: "error", text: "Paste at least one student: college ID, email." });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const token = await getToken();
      setJob(await examEndpoints.rosterDryRun(token, selectedId, parsed.rows));
    } catch (err) {
      fail(err, "Could not check the roster.");
    } finally {
      setBusy(false);
    }
  };

  const handleApply = async () => {
    if (!job) return;
    setBusy(true);
    setNotice(null);
    try {
      const token = await getToken();
      let current = job;
      const maxCalls = Math.ceil(rosterRows.length / APPLY_BATCH_SIZE) + 1;
      for (let call = 0; call < maxCalls && current.status !== "completed"; call += 1) {
        current = await examEndpoints.rosterApply(token, selectedId, job.id, rosterRows);
      }
      setJob(null);
      setRosterText("");
      setRosterRows([]);
      await loadMembers(selectedId);
      setNotice({ kind: "ok", text: "Roster applied." });
    } catch (err) {
      fail(err, "Could not apply the roster.");
    } finally {
      setBusy(false);
    }
  };

  const handleRevoke = async (identityKey: string) => {
    if (!window.confirm(`Revoke ${identityKey}? They lose access to exams immediately.`)) return;
    setBusy(true);
    setNotice(null);
    try {
      const token = await getToken();
      await examEndpoints.revokeMembership(token, selectedId, identityKey);
      await loadMembers(selectedId);
      setNotice({ kind: "ok", text: `Revoked ${identityKey}.` });
    } catch (err) {
      fail(err, "Could not revoke the membership.");
    } finally {
      setBusy(false);
    }
  };

  const handleReauth = async () => {
    await signOut(auth);
    await refresh();
    router.replace("/exams/login");
  };

  if (loadError) return <ErrorState title="Could not load institutions" description={loadError} />;
  if (!institutions) return <LoadingState message="Loading institutions…" />;

  const rowProblems = job ? job.row_results.filter((result) => !result.ok) : [];
  const rowsOk = job ? job.row_results.filter((result) => result.ok).length : 0;

  return (
    <div className="page-container py-10">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Institution &amp; roster</h1>
        <Link href="/exams/faculty" className="btn-secondary">Back to exams</Link>
      </div>

      {notice && (
        <p
          role={notice.kind === "error" ? "alert" : "status"}
          className={`mt-4 text-sm ${notice.kind === "error" ? "text-[var(--error)]" : "text-[var(--success)]"}`}
        >
          {notice.text}
        </p>
      )}
      {needsReauth && (
        <div className="card mt-4 p-4 text-sm">
          <p>This action needs a fresh sign-in. Sign in again, then repeat it.</p>
          <button className="btn-primary mt-3" onClick={handleReauth}>Sign in again</button>
        </div>
      )}

      {institutions.is_platform_operator && (
        <form onSubmit={handleCreateInstitution} className="card mt-6 flex flex-wrap gap-3 p-4">
          <input
            className="input-field min-w-[16rem] flex-1"
            placeholder="New institution name"
            aria-label="New institution name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button className="btn-primary" type="submit" disabled={busy}>Create institution</button>
        </form>
      )}

      {institutions.results.length === 0 ? (
        <p className="mt-6 text-sm text-[var(--text-muted)]">No institutions yet.</p>
      ) : (
        <>
          <div className="mt-6 flex items-center gap-3">
            <label htmlFor="institution" className="label mb-0">Institution</label>
            <select
              id="institution"
              className="input-field"
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
            >
              {institutions.results.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          </div>
          {selected && (
            <p className="mt-3 text-sm text-[var(--text-muted)]">
              Student sign-in link:{" "}
              <code className="break-all" data-testid="student-signin-link">
                {`${window.location.origin}/exams/login?institution=${selected.id}`}
              </code>
            </p>
          )}

          <section className="card mt-4 p-5">
            <h2 className="text-lg font-semibold">Add students</h2>
            <p className="mt-1 text-sm text-[var(--text-muted)]">
              One student per line: <code>college ID, email</code>. Check the list first; nothing is saved until you apply it.
            </p>
            <textarea
              className="input-field mt-3 w-full font-mono text-sm"
              rows={6}
              aria-label="Roster rows"
              placeholder={"21CS001, student1@college.edu\n21CS002, student2@college.edu"}
              value={rosterText}
              onChange={(e) => {
                setRosterText(e.target.value);
                setJob(null);
              }}
            />
            <div className="mt-3 flex gap-3">
              <button className="btn-secondary" onClick={handleDryRun} disabled={busy || !rosterText.trim()}>
                Check roster
              </button>
              <button className="btn-primary" onClick={handleApply} disabled={busy || !job || rowsOk === 0}>
                Apply roster
              </button>
            </div>
            {job && (
              <div className="mt-3 text-sm" data-testid="roster-check">
                <p>
                  {rowsOk} of {job.total_rows} rows can be applied
                  {rowProblems.length > 0 ? `; ${rowProblems.length} need attention and will be skipped.` : "."}
                </p>
                {rowProblems.length > 0 && (
                  <ul className="mt-2 list-disc pl-5 text-[var(--text-muted)]">
                    {rowProblems.slice(0, 20).map((result) => (
                      <li key={result.row_index}>
                        Row {result.row_index + 1} ({result.identity_key}): {result.error}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </section>

          <section className="card mt-4 p-5">
            <h2 className="text-lg font-semibold">Members{selected ? ` of ${selected.name}` : ""}</h2>
            {members === null ? (
              <p className="mt-2 text-sm text-[var(--text-muted)]">Loading members…</p>
            ) : members.length === 0 ? (
              <p className="mt-2 text-sm text-[var(--text-muted)]">No members yet.</p>
            ) : (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-[var(--text-muted)]">
                    <tr>
                      <th className="py-1 pr-3">College ID</th>
                      <th className="py-1 pr-3">Role</th>
                      <th className="py-1 pr-3">Status</th>
                      <th className="py-1 pr-3">Signed in</th>
                      {selected?.can_administer && <th className="py-1 pr-3">Email</th>}
                      {selected?.can_administer && <th className="py-1" />}
                    </tr>
                  </thead>
                  <tbody>
                    {members.map((member) => (
                      <tr key={member.identity_key} className="border-t border-[var(--border)]">
                        <td className="py-1 pr-3 font-mono">{member.identity_key}</td>
                        <td className="py-1 pr-3">{member.role_scope}</td>
                        <td className="py-1 pr-3">{member.status}</td>
                        <td className="py-1 pr-3">{member.linked ? "yes" : "not yet"}</td>
                        {selected?.can_administer && <td className="py-1 pr-3">{member.contact_email}</td>}
                        {selected?.can_administer && (
                          <td className="py-1">
                            {member.status === "active" && (
                              <button
                                className="btn-secondary"
                                onClick={() => handleRevoke(member.identity_key)}
                                disabled={busy}
                              >
                                Revoke
                              </button>
                            )}
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

export default function InstitutionAdminPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <InstitutionAdmin />
    </ExamProtectedRoute>
  );
}
