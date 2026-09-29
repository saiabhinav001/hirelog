"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { ProtectedRoute } from "@/components/ProtectedRoute";
import { useAuth } from "@/context/AuthContext";
import { apiFetch } from "@/lib/api";
import { getClientAuthToken } from "@/lib/authToken";
import type { Experience } from "@/lib/types";

export default function ExperienceDetailPage() {
  const { user, loading: authLoading } = useAuth();
  const params = useParams<{ id: string }>();
  const experienceId = useMemo(() => String(params?.id || ""), [params]);

  const [experience, setExperience] = useState<Experience | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (authLoading) {
      return;
    }

    if (!experienceId || !user) {
      setLoading(false);
      return;
    }

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const token = await getClientAuthToken();
        if (!token) {
          throw new Error("Not authenticated.");
        }
        const response = await apiFetch<Experience>(`/api/experiences/${experienceId}`, { method: "GET" }, token);
        setExperience(response);
      } catch {
        setError("Could not load this experience.");
      } finally {
        setLoading(false);
      }
    };

    load();
  }, [authLoading, experienceId, user]);

  return (
    <ProtectedRoute>
      <div className="page-container py-12">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-2xl font-semibold">Experience Details</h1>
          <Link href="/search" className="btn-secondary text-sm">Back to Search</Link>
        </div>

        {loading ? (
          <div className="mt-6 space-y-4">
            <div className="skeleton skeleton-heading w-64" />
            <div className="skeleton skeleton-card" />
            <div className="skeleton skeleton-card" />
          </div>
        ) : error ? (
          <div className="mt-6 card p-6 max-w-lg text-center">
            <p className="text-sm text-[var(--text-muted)]">{error}</p>
            <Link href="/search" className="btn-primary mt-4 inline-flex">Go to Search</Link>
          </div>
        ) : experience ? (
          <div className="mt-6 space-y-6">
            <section className="card p-5">
              <h2 className="text-lg font-semibold">{experience.company}</h2>
              <p className="mt-1 text-sm text-[var(--text-muted)]">
                {experience.role} · {experience.round} · {experience.year} · {experience.difficulty}
              </p>
              {experience.summary ? (
                <p className="mt-4 text-sm leading-6 text-[var(--text-secondary)]">{experience.summary}</p>
              ) : (
                <p className="mt-4 text-sm text-[var(--text-muted)]">No summary available yet.</p>
              )}
            </section>

            <section className="card p-5">
              <h3 className="text-base font-semibold">Topics</h3>
              {experience.topics?.length ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {experience.topics.map((topic) => (
                    <span key={topic} className="badge">{topic}</span>
                  ))}
                </div>
              ) : (
                <p className="mt-3 text-sm text-[var(--text-muted)]">No topics extracted yet.</p>
              )}
            </section>

            <section className="card p-5">
              <h3 className="text-base font-semibold">Questions</h3>
              {(experience.extracted_questions || []).length ? (
                <ul className="mt-3 space-y-2">
                  {experience.extracted_questions.map((entry, index) => {
                    const question = entry.question_text || entry.question || "";
                    return (
                      <li key={`${index}-${question.slice(0, 24)}`} className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-3 text-sm">
                        <p>{question}</p>
                        <p className="mt-1 text-xs text-[var(--text-muted)]">{entry.topic || "General"}</p>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="mt-3 text-sm text-[var(--text-muted)]">No questions extracted yet.</p>
              )}
            </section>
          </div>
        ) : null}
      </div>
    </ProtectedRoute>
  );
}
