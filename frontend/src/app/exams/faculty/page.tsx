"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type ExamDefinition } from "@/lib/examEndpoints";

function FacultyHome() {
  const { getToken } = useExamAuth();
  const [definitions, setDefinitions] = useState<ExamDefinition[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.listMyDefinitions(token);
      setDefinitions(data.results);
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
      await examEndpoints.createDefinition(token, title.trim());
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
      <h1 className="text-2xl font-semibold">Your exam courses</h1>

      <form onSubmit={handleCreate} className="card mt-6 flex gap-3 p-4">
        <input
          className="input-field flex-1"
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
