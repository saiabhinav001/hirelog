"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type ExamDefinition, type InstitutionList } from "@/lib/examEndpoints";

function FacultyHome() {
  const { getToken } = useExamAuth();
  const [definitions, setDefinitions] = useState<ExamDefinition[] | null>(null);
  const [institutions, setInstitutions] = useState<InstitutionList | null>(null);
  const [institutionId, setInstitutionId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const [data, institutionData] = await Promise.all([
        examEndpoints.listMyDefinitions(token),
        examEndpoints.listInstitutions(token),
      ]);
      setDefinitions(data.results);
      setInstitutions(institutionData);
      setInstitutionId((current) => current || (institutionData.results.length === 1 ? institutionData.results[0].id : ""));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load your courses.");
    }
  }, [getToken]);

  useEffect(() => {
    load();
  }, [load]);

  const handleCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!title.trim()) return;
    setCreating(true);
    try {
      const token = await getToken();
      await examEndpoints.createDefinition(token, title.trim(), institutionId || undefined);
      setTitle("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create exam definition.");
    } finally {
      setCreating(false);
    }
  };

  if (error) return <ErrorState title="Could not load courses" description={error} />;
  if (definitions === null) return <LoadingState message="Loading your courses…" />;

  return (
    <div className="page-container py-10">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Your exam courses</h1>
        {institutions && institutions.results.length > 0 && (
          <Link href="/exams/faculty/institution" className="btn-secondary">
            Institution &amp; roster
          </Link>
        )}
      </div>

      <form onSubmit={handleCreate} className="card mt-6 flex flex-wrap gap-3 p-4">
        {institutions && institutions.results.length > 0 && (
          <select
            className="input-field"
            aria-label="Institution for the new exam"
            value={institutionId}
            onChange={(e) => setInstitutionId(e.target.value)}
            required={!institutions.is_platform_operator}
          >
            {institutions.is_platform_operator && <option value="">No institution (pilot exam)</option>}
            {!institutions.is_platform_operator && institutionId === "" && <option value="">Choose an institution</option>}
            {institutions.results.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        )}
        <input
          className="input-field min-w-[16rem] flex-1"
          placeholder="New exam title, e.g. Midterm — Data Structures"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          aria-label="New exam title"
        />
        <button className="btn-primary" type="submit" disabled={creating}>
          {creating ? "Creating…" : "Create"}
        </button>
      </form>

      {definitions.length === 0 ? (
        <div className="mt-6">
          <EmptyState title="No exams yet" description="Create your first exam definition above to get started." />
        </div>
      ) : (
        <div className="mt-6 grid gap-4 sm:grid-cols-2">
          {definitions.map((def) => (
            <Link key={def.id} href={`/exams/faculty/${def.id}`} className="card p-5 transition hover:border-[var(--border-hover)]">
              <h2 className="font-semibold">{def.title}</h2>
              <p className="mt-2 text-sm text-[var(--text-muted)]">
                {def.latest_published_revision_id ? "Published" : "Draft — not yet published"}
              </p>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export default function FacultyHomePage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <FacultyHome />
    </ExamProtectedRoute>
  );
}
