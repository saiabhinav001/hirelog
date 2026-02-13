"use client";

import { useState, useEffect } from "react";

import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { apiFetch } from "@/lib/api";
import { auth } from "@/lib/firebase";
import type { PracticeList } from "@/lib/types";

type SaveToListButtonProps = {
  questionText: string;
  topic?: string;
  difficulty?: string;
  sourceExperienceId?: string;
  sourceCompany?: string;
};

export function SaveToListButton({
  questionText,
  topic = "General",
  difficulty,
  sourceExperienceId,
  sourceCompany,
}: SaveToListButtonProps) {
  const { user } = useAuth();
  const { toast } = useToast();
  const [isOpen, setIsOpen] = useState(false);
  const [lists, setLists] = useState<PracticeList[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [newListName, setNewListName] = useState("");
  const [showNewList, setShowNewList] = useState(false);

  useEffect(() => {
    if (isOpen && user && auth.currentUser) {
      setLoading(true);
      auth.currentUser.getIdToken().then((token) => {
        apiFetch<PracticeList[]>("/api/practice-lists", { method: "GET" }, token)
          .then(setLists)
          .finally(() => setLoading(false));
      });
    }
  }, [isOpen, user]);

  if (!user) return null;

  const handleSaveToList = async (listId: string) => {
    if (!auth.currentUser) return;
    setSaving(listId);
    try {
      const token = await auth.currentUser.getIdToken();
      await apiFetch(
        `/api/practice-lists/${listId}/questions`,
        {
          method: "POST",
          body: JSON.stringify({
            question_text: questionText,
            topic,
            difficulty,
            source: sourceExperienceId ? "interview_experience" : "manual",
            source_experience_id: sourceExperienceId,
            source_company: sourceCompany,
          }),
        },
        token
      );
      // Optimistic: bump the local question count for the saved list
      const listName = lists.find((l) => l.id === listId)?.name ?? "list";
      setLists((prev) =>
        prev.map((l) =>
          l.id === listId ? { ...l, question_count: (l.question_count ?? 0) + 1 } : l
        )
      );
      setSaved(true);
      setIsOpen(false);
      toast(
        `Added to '${listName}'.`,
        "success",
        { label: "Open practice →", href: "/practice" }
      );
      setTimeout(() => setSaved(false), 500);
    } finally {
      setSaving(null);
    }
  };

  const handleCreateList = async () => {
    if (!auth.currentUser || !newListName.trim()) return;
    setSaving("new");
    try {
      const token = await auth.currentUser.getIdToken();
      const newList = await apiFetch<PracticeList>(
        "/api/practice-lists",
        {
          method: "POST",
          body: JSON.stringify({ name: newListName.trim() }),
        },
        token
      );
      setLists((prev) => [newList, ...prev]);
      setNewListName("");
      setShowNewList(false);
      await handleSaveToList(newList.id);
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="p-1.5 rounded-md text-[var(--text-muted)] hover:text-[var(--primary)] hover:bg-[var(--primary-soft)] transition-colors"
        title="Save to practice list"
      >
        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
        </svg>
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setIsOpen(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 w-64 rounded-lg border border-[var(--border)] bg-[var(--surface)] shadow-lg">
            <div className="p-2 border-b border-[var(--border)]">
              <p className="text-xs font-medium text-[var(--text-muted)] px-2 py-1">
                Save to practice list
              </p>
            </div>

            {saved ? (
              <div className="p-4 text-center text-sm text-[var(--success)]">
                ✓ Saved to list
              </div>
            ) : loading ? (
              <div className="p-4 text-center text-sm text-[var(--text-muted)]">
                Loading...
              </div>
            ) : (
              <div className="max-h-48 overflow-y-auto">
                {lists.length === 0 && !showNewList ? (
                  <div className="p-3 text-center">
                    <p className="text-sm text-[var(--text-muted)]">No lists yet</p>
                    <button
                      onClick={() => setShowNewList(true)}
                      className="mt-2 text-sm text-[var(--primary)] hover:underline"
                    >
                      Create your first list
                    </button>
                  </div>
                ) : (
                  <>
                    {lists.map((list) => (
                      <button
                        key={list.id}
                        onClick={() => handleSaveToList(list.id)}
                        disabled={saving !== null}
                        className="w-full px-3 py-2 text-left text-sm hover:bg-[var(--surface-muted)] flex items-center justify-between disabled:opacity-50"
                      >
                        <span>{list.name}</span>
                        {saving === list.id && (
                          <span className="text-xs text-[var(--text-muted)]">Saving...</span>
                        )}
                      </button>
                    ))}
                  </>
                )}
              </div>
            )}

            {!saved && (
              <div className="p-2 border-t border-[var(--border)]">
                {showNewList ? (
                  <div className="flex gap-2">
                    <input
                      className="input-field flex-1 text-sm !h-8"
                      placeholder="List name"
                      value={newListName}
                      onChange={(e) => setNewListName(e.target.value)}
                      autoFocus
                    />
                    <button
                      onClick={handleCreateList}
                      disabled={!newListName.trim() || saving !== null}
                      className="btn-primary text-xs !h-8"
                    >
                      {saving === "new" ? "..." : "Add"}
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => setShowNewList(true)}
                    className="w-full px-3 py-2 text-left text-sm text-[var(--primary)] hover:bg-[var(--surface-muted)] flex items-center gap-2"
                  >
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                    </svg>
                    Create new list
                  </button>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
