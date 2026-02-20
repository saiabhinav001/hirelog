"use client";

import { useState, useEffect, useCallback, useRef } from "react";

import { ProtectedRoute } from "@/components/ProtectedRoute";
import { useAuth } from "@/context/AuthContext";
import { apiFetch } from "@/lib/api";
import { auth } from "@/lib/firebase";
import type { PracticeList, PracticeQuestion, QuestionStatus } from "@/lib/types";

const TOPICS = ["DSA", "DBMS", "OS", "CN", "OOP", "HR", "System Design", "General"];

// ─────────────────────────────────────────────────────────────────────────────
// localStorage stale-while-revalidate cache
// ─────────────────────────────────────────────────────────────────────────────

const PRACTICE_CACHE_KEY = "hirelog_practice_cache";
const PRACTICE_CACHE_TTL = 5 * 60 * 1000; // 5 minutes

interface PracticeCache {
  ts: number;
  lists: PracticeList[];
}

function getPracticeCache(): PracticeCache | null {
  try {
    const raw = localStorage.getItem(PRACTICE_CACHE_KEY);
    if (!raw) return null;
    const cache: PracticeCache = JSON.parse(raw);
    if (Date.now() - cache.ts > PRACTICE_CACHE_TTL) {
      localStorage.removeItem(PRACTICE_CACHE_KEY);
      return null;
    }
    return cache;
  } catch {
    return null;
  }
}

function setPracticeCache(lists: PracticeList[]) {
  try {
    localStorage.setItem(PRACTICE_CACHE_KEY, JSON.stringify({ ts: Date.now(), lists }));
  } catch { /* quota exceeded — ignore */ }
}

function ListSkeleton() {
  return (
    <div className="space-y-3">
      {[1, 2, 3].map((i) => (
        <div key={i} className="skeleton h-20 rounded-lg" />
      ))}
    </div>
  );
}

function EmptyListState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="card p-8 text-center">
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-[var(--surface-muted)]">
        <svg className="h-6 w-6 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" />
        </svg>
      </div>
      <h3 className="text-lg font-semibold">No practice lists yet</h3>
      <p className="mt-2 text-sm text-[var(--text-muted)]">
        Create your first list to start organizing interview questions.
      </p>
      <button onClick={onCreate} className="btn-primary mt-4">
        Create list
      </button>
    </div>
  );
}

function CreateListModal({ 
  isOpen, 
  onClose, 
  onCreate 
}: { 
  isOpen: boolean; 
  onClose: () => void; 
  onCreate: (name: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    try {
      await onCreate(name.trim());
      setName("");
      onClose();
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="card w-full max-w-md p-6">
        <h2 className="text-lg font-semibold">Create practice list</h2>
        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <div>
            <label className="label">List name</label>
            <input
              className="input-field"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g., JP Morgan Practice"
              autoFocus
            />
          </div>
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={onClose} className="btn-ghost">
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={loading || !name.trim()}>
              {loading ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function AddQuestionModal({ 
  isOpen, 
  onClose, 
  onAdd,
  listName,
}: { 
  isOpen: boolean; 
  onClose: () => void; 
  onAdd: (question: { question_text: string; topic: string; difficulty?: string }) => Promise<void>;
  listName: string;
}) {
  const [questionText, setQuestionText] = useState("");
  const [topic, setTopic] = useState("General");
  const [difficulty, setDifficulty] = useState("");
  const [loading, setLoading] = useState(false);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!questionText.trim()) return;
    setLoading(true);
    try {
      await onAdd({ 
        question_text: questionText.trim(), 
        topic, 
        difficulty: difficulty || undefined 
      });
      setQuestionText("");
      setTopic("General");
      setDifficulty("");
      onClose();
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="card w-full max-w-lg p-6">
        <h2 className="text-lg font-semibold">Add question to {listName}</h2>
        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <div>
            <label className="label">Question</label>
            <textarea
              className="input-field min-h-[100px]"
              value={questionText}
              onChange={(e) => setQuestionText(e.target.value)}
              placeholder="Enter the interview question..."
              autoFocus
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label">Topic</label>
              <select
                className="input-field"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
              >
                {TOPICS.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="label">Difficulty (optional)</label>
              <select
                className="input-field"
                value={difficulty}
                onChange={(e) => setDifficulty(e.target.value)}
              >
                <option value="">Not set</option>
                <option value="Easy">Easy</option>
                <option value="Medium">Medium</option>
                <option value="Hard">Hard</option>
              </select>
            </div>
          </div>
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={onClose} className="btn-ghost">
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={loading || !questionText.trim()}>
              {loading ? "Adding..." : "Add question"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function QuestionCard({ 
  question, 
  onStatusChange,
  onDelete,
}: { 
  question: PracticeQuestion;
  onStatusChange: (status: QuestionStatus) => void;
  onDelete: () => void;
}) {
  const statusStyles: Record<QuestionStatus, string> = {
    unvisited: "bg-[var(--surface-muted)] text-[var(--text-muted)] hover:bg-[var(--border)]",
    practicing: "bg-[var(--warning-soft)] text-[var(--warning)] hover:bg-[var(--warning)]/20",
    revised: "bg-[var(--success-soft)] text-[var(--success)] hover:bg-[var(--success)]/20",
  };

  const nextStatus: Record<QuestionStatus, QuestionStatus> = {
    unvisited: "practicing",
    practicing: "revised",
    revised: "unvisited",
  };

  return (
    <div className="card p-4 group">
      <div className="flex items-start gap-3">
        <button
          onClick={() => onStatusChange(nextStatus[question.status])}
          className={`mt-0.5 shrink-0 inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors cursor-pointer ${statusStyles[question.status]}`}
          aria-label={`Status: ${question.status}. Click to cycle to ${nextStatus[question.status]}.`}
        >
          {question.status}
          <svg className="h-3 w-3 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </button>
        <div className="flex-1 min-w-0">
          <p className="text-sm">{question.question_text}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--text-muted)]">
            <span className="badge">{question.topic}</span>
            {question.difficulty && (
              <span className={`badge ${
                question.difficulty === "Easy" ? "badge-success" :
                question.difficulty === "Hard" ? "badge-error" : "badge-warning"
              }`}>
                {question.difficulty}
              </span>
            )}
            {question.source === "interview_experience" && question.source_company && (
              <span>from {question.source_company}</span>
            )}
          </div>
        </div>
        <button
          onClick={onDelete}
          className="shrink-0 p-1 text-[var(--text-muted)] hover:text-[var(--error)] opacity-0 group-hover:opacity-100 transition-opacity"
          aria-label="Delete question"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  );
}

/** Compute updated list stats from local question state (avoids refetching all lists). */
function computeListStats(base: PracticeList, questions: PracticeQuestion[]): PracticeList {
  const total = questions.length;
  const revised = questions.filter((q) => q.status === "revised").length;
  const practicing = questions.filter((q) => q.status === "practicing").length;
  const unvisited = total - revised - practicing;
  const topicDist: Record<string, number> = {};
  for (const q of questions) {
    topicDist[q.topic] = (topicDist[q.topic] || 0) + 1;
  }
  return {
    ...base,
    question_count: total,
    revised_count: revised,
    practicing_count: practicing,
    unvisited_count: unvisited,
    topic_distribution: topicDist,
    revised_percent: total > 0 ? Math.round((revised / total) * 1000) / 10 : 0,
  };
}

function ListDetail({ 
  list, 
  onBack,
  onRefresh,
  onListUpdate,
}: { 
  list: PracticeList; 
  onBack: () => void;
  onRefresh: () => void;
  onListUpdate: (updated: PracticeList) => void;
}) {
  const [questions, setQuestions] = useState<PracticeQuestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAddModal, setShowAddModal] = useState(false);

  // Keep a ref to current questions so optimistic helpers always see latest
  const questionsRef = useRef(questions);
  questionsRef.current = questions;

  // Token caching — avoid repeated getIdToken() calls (~20-50ms each)
  const tokenRef = useRef<{ token: string; ts: number } | null>(null);
  const TOKEN_TTL = 5 * 60 * 1000; // 5 minutes
  const getToken = useCallback(async (): Promise<string> => {
    const cached = tokenRef.current;
    if (cached && Date.now() - cached.ts < TOKEN_TTL) return cached.token;
    if (!auth.currentUser) throw new Error("Not authenticated");
    const token = await auth.currentUser.getIdToken();
    tokenRef.current = { token, ts: Date.now() };
    return token;
  }, []);

  // Request dedup — prevents concurrent duplicate mutations from rapid clicks
  const pendingOps = useRef(new Set<string>());

  const loadQuestions = useCallback(async () => {
    if (!auth.currentUser) return;
    setLoading(true);
    try {
      const token = await getToken();
      const data = await apiFetch<PracticeQuestion[]>(
        `/api/practice-lists/${list.id}/questions`,
        { method: "GET" },
        token
      );
      setQuestions(data);
    } finally {
      setLoading(false);
    }
  }, [list.id, getToken]);

  useEffect(() => {
    loadQuestions();
  }, [loadQuestions]);

  /** Push local question array into list stats + parent state. */
  const syncListStats = useCallback(
    (nextQuestions: PracticeQuestion[]) => {
      const updated = computeListStats(list, nextQuestions);
      onListUpdate(updated);
    },
    [list, onListUpdate],
  );

  const handleAddQuestion = async (q: { question_text: string; topic: string; difficulty?: string }) => {
    if (!auth.currentUser) return;
    const opKey = `add:${q.question_text}`;
    if (pendingOps.current.has(opKey)) return;
    pendingOps.current.add(opKey);
    try {
      const token = await getToken();
      const created = await apiFetch<PracticeQuestion>(
        `/api/practice-lists/${list.id}/questions`,
        {
          method: "POST",
          body: JSON.stringify({ ...q, source: "manual" }),
        },
        token
      );
      // Optimistic: append the server-returned question
      const next = [...questionsRef.current, created];
      setQuestions(next);
      syncListStats(next);
      // Background: reconcile list cache (non-blocking)
      onRefresh();
    } catch (error) {
      console.error("Failed to add question:", error);
      throw error; // Re-throw so modal can show error state
    } finally {
      pendingOps.current.delete(opKey);
    }
  };

  const handleStatusChange = async (questionId: string, status: QuestionStatus) => {
    if (!auth.currentUser || !questionId) return;

    // Dedup: skip if this exact operation is already in-flight
    const opKey = `status:${questionId}:${status}`;
    if (pendingOps.current.has(opKey)) return;
    pendingOps.current.add(opKey);

    // Optimistic update
    const prev = questionsRef.current;
    const next = prev.map((q) => (q.id === questionId ? { ...q, status } : q));
    setQuestions(next);
    syncListStats(next);

    try {
      const token = await getToken();
      await apiFetch(
        `/api/practice-lists/${list.id}/questions/${questionId}`,
        {
          method: "PUT",
          body: JSON.stringify({ status }),
        },
        token
      );
    } catch (error) {
      console.error("Failed to update question status:", error);
      // Rollback + resync
      setQuestions(prev);
      syncListStats(prev);
      await loadQuestions();
    } finally {
      pendingOps.current.delete(opKey);
    }
  };

  const handleDeleteQuestion = async (questionId: string) => {
    if (!auth.currentUser || !questionId) return;

    // Dedup: skip if already deleting
    const opKey = `delete:${questionId}`;
    if (pendingOps.current.has(opKey)) return;
    pendingOps.current.add(opKey);

    // Optimistic update
    const prev = questionsRef.current;
    const next = prev.filter((q) => q.id !== questionId);
    setQuestions(next);
    syncListStats(next);

    try {
      const token = await getToken();
      await apiFetch(
        `/api/practice-lists/${list.id}/questions/${questionId}`,
        { method: "DELETE" },
        token
      );
    } catch (error) {
      console.error("Failed to delete question:", error);
      // Rollback + resync
      setQuestions(prev);
      syncListStats(prev);
      await loadQuestions();
    } finally {
      pendingOps.current.delete(opKey);
    }
  };

  return (
    <div>
      <button onClick={onBack} className="btn-ghost text-sm mb-4">
        ← Back to lists
      </button>

      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{list.name}</h1>
          <p className="mt-1 text-[var(--text-muted)]">
            {list.question_count} question{list.question_count !== 1 ? "s" : ""}
            {list.question_count > 0 && (
              <span>
                {" "}· {list.revised_count} revised · {list.practicing_count} practicing · {list.unvisited_count} unvisited
              </span>
            )}
          </p>
        </div>
        <button onClick={() => setShowAddModal(true)} className="btn-primary">
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
          </svg>
          Add question
        </button>
      </div>

      {loading ? (
        <div className="mt-6 space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="skeleton h-20 rounded-lg" />
          ))}
        </div>
      ) : questions.length === 0 ? (
        <div className="mt-8 card p-8 text-center">
          <p className="text-[var(--text-muted)]">Add questions that you remember</p>
          <button onClick={() => setShowAddModal(true)} className="btn-primary mt-4">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            Add first question
          </button>
        </div>
      ) : (
        <div className="mt-6 space-y-3">
          {questions.map((q) => (
            <QuestionCard
              key={q.id}
              question={q}
              onStatusChange={(status) => handleStatusChange(q.id, status)}
              onDelete={() => handleDeleteQuestion(q.id)}
            />
          ))}
        </div>
      )}

      <AddQuestionModal
        isOpen={showAddModal}
        onClose={() => setShowAddModal(false)}
        onAdd={handleAddQuestion}
        listName={list.name}
      />
    </div>
  );
}

export default function PracticePage() {
  const { user } = useAuth();
  const [lists, setLists] = useState<PracticeList[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedList, setSelectedList] = useState<PracticeList | null>(null);
  const fetchedRef = useRef(false);

  const loadLists = useCallback(async () => {
    if (!auth.currentUser) return;
    try {
      const token = await auth.currentUser.getIdToken();
      const data = await apiFetch<PracticeList[]>(
        "/api/practice-lists",
        { method: "GET" },
        token
      );
      setLists(data);
      setPracticeCache(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!user) return;
    if (fetchedRef.current) return;
    fetchedRef.current = true;

    // 1. Restore stale data from cache for instant paint
    const cached = getPracticeCache();
    if (cached?.lists) {
      setLists(cached.lists);
      setLoading(false);
    }

    // 2. Revalidate in background
    loadLists();
  }, [user, loadLists]);

  const handleCreateList = async (name: string) => {
    if (!auth.currentUser) return;
    const token = await auth.currentUser.getIdToken();
    const newList = await apiFetch<PracticeList>(
      "/api/practice-lists",
      {
        method: "POST",
        body: JSON.stringify({ name }),
      },
      token
    );
    setLists((prev) => [newList, ...prev]);
  };

  const handleDeleteList = async (listId: string) => {
    if (!auth.currentUser) return;
    if (!confirm("Delete this list and all its questions?")) return;
    const token = await auth.currentUser.getIdToken();
    await apiFetch(`/api/practice-lists/${listId}`, { method: "DELETE" }, token);
    setLists((prev) => prev.filter((l) => l.id !== listId));
  };

  return (
    <ProtectedRoute>
      <div className="page-container py-12">
        {selectedList ? (
          <ListDetail
            list={selectedList}
            onBack={() => {
              setSelectedList(null);
              loadLists();
            }}
            onRefresh={loadLists}
            onListUpdate={(updated) => {
              setSelectedList(updated);
              setLists((prev) =>
                prev.map((l) => (l.id === updated.id ? updated : l))
              );
            }}
          />
        ) : (
          <>
            <div className="flex items-baseline justify-between">
              <div>
                <h1 className="text-2xl font-semibold">Practice Lists</h1>
                <p className="mt-1 text-[var(--text-muted)]">
                  Organize and track interview questions for revision.
                </p>
              </div>
              {lists.length > 0 && (
                <button onClick={() => setShowCreateModal(true)} className="btn-primary">
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                  </svg>
                  New list
                </button>
              )}
            </div>

            <div className="mt-8">
              {loading ? (
                <ListSkeleton />
              ) : lists.length === 0 ? (
                <EmptyListState onCreate={() => setShowCreateModal(true)} />
              ) : (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {lists.map((list) => {
                    const progressPercent = list.question_count > 0 ? list.revised_percent : 0;
                    return (
                      <div
                        key={list.id}
                        className="card p-5 cursor-pointer hover:bg-[var(--surface-hover)] transition-colors group"
                        onClick={() => setSelectedList(list)}
                      >
                        <div className="flex items-start justify-between">
                          <h3 className="font-medium">{list.name}</h3>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteList(list.id);
                            }}
                            className="p-1 text-[var(--text-muted)] hover:text-[var(--error)] opacity-0 group-hover:opacity-100 transition-opacity"
                            title="Delete list"
                          >
                            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                            </svg>
                          </button>
                        </div>
                        
                        {/* Preparation Progress */}
                        {list.question_count > 0 && (
                          <div className="mt-3">
                            <div className="flex items-center justify-between text-xs mb-1">
                              <span className="text-[var(--text-muted)]">Preparation progress</span>
                              <span className={progressPercent >= 80 ? "text-[var(--success)]" : progressPercent >= 40 ? "text-[var(--warning)]" : "text-[var(--text-muted)]"}>
                                {Math.round(progressPercent)}%
                              </span>
                            </div>
                            <div className="h-1.5 w-full bg-[var(--surface-muted)] rounded-full overflow-hidden">
                              <div 
                                className={`h-full rounded-full transition-all ${progressPercent >= 80 ? "bg-[var(--success)]" : progressPercent >= 40 ? "bg-[var(--warning)]" : "bg-[var(--text-muted)]"}`}
                                style={{ width: `${progressPercent}%` }}
                              />
                            </div>
                          </div>
                        )}
                        
                        <div className="mt-3 flex items-center gap-3 text-sm text-[var(--text-muted)]">
                          <span>{list.question_count} question{list.question_count !== 1 ? 's' : ''}</span>
                          {list.question_count > 0 && (
                            <>
                              <span className="text-[var(--success)]">{list.revised_count} revised</span>
                              <span className="text-[var(--warning)]">{list.practicing_count} practicing</span>
                            </>
                          )}
                        </div>
                        {Object.keys(list.topic_distribution).length > 0 && (
                          <div className="mt-3 flex flex-wrap gap-1">
                            {Object.entries(list.topic_distribution).slice(0, 3).map(([topic, count]) => (
                              <span key={topic} className="badge text-xs">
                                {topic} {count}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </>
        )}

        <CreateListModal
          isOpen={showCreateModal}
          onClose={() => setShowCreateModal(false)}
          onCreate={handleCreateList}
        />
      </div>
    </ProtectedRoute>
  );
}
