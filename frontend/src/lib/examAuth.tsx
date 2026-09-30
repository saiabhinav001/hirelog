"use client";

/**
 * Exam-specific auth context — deliberately separate from `AuthContext`
 * (legacy role hierarchy). Role routing is based on SERVER capabilities
 * from `/api/exams/v1/me`, never a client-side guess (contract: "Role
 * routing is based on server capabilities").
 */

import { onAuthStateChanged, type User } from "firebase/auth";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { auth } from "@/lib/firebase";
import { examApiFetch } from "@/lib/examsApi";

export type ExamMembership = {
  institution_id: string;
  role_scope: "student" | "faculty" | "proctor" | "admin";
  status: "active" | "revoked";
  authorization_version: number;
};

export type ExamCapabilities = {
  uid: string;
  email: string | null;
  is_bootstrap_faculty: boolean;
  memberships: ExamMembership[];
  has_any_exam_membership: boolean;
};

type ExamAuthContextValue = {
  user: User | null;
  capabilities: ExamCapabilities | null;
  loading: boolean;
  isFaculty: boolean;
  isStudent: boolean;
  getToken: () => Promise<string | undefined>;
  refresh: () => Promise<void>;
};

const ExamAuthContext = createContext<ExamAuthContextValue | null>(null);

export function ExamAuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [capabilities, setCapabilities] = useState<ExamCapabilities | null>(null);
  const [loading, setLoading] = useState(true);

  const getToken = useCallback(async () => {
    return auth.currentUser?.getIdToken();
  }, []);

  const refresh = useCallback(async () => {
    if (!auth.currentUser) {
      setCapabilities(null);
      return;
    }
    try {
      const token = await auth.currentUser.getIdToken();
      const data = await examApiFetch<ExamCapabilities>("/api/exams/v1/me", { method: "GET" }, token);
      setCapabilities(data);
    } catch (err) {
      console.warn("Could not load exam capabilities:", (err as Error).message);
      setCapabilities(null);
    }
  }, []);

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (firebaseUser) => {
      setUser(firebaseUser);
      if (firebaseUser) {
        await refresh();
      } else {
        setCapabilities(null);
      }
      setLoading(false);
    });
    return () => unsubscribe();
  }, [refresh]);

  const isFaculty = useMemo(
    () =>
      Boolean(
        capabilities?.is_bootstrap_faculty ||
          capabilities?.memberships.some((m) => m.status === "active" && (m.role_scope === "faculty" || m.role_scope === "admin"))
      ),
    [capabilities]
  );
  const isStudent = useMemo(
    () => Boolean(capabilities?.memberships.some((m) => m.status === "active" && m.role_scope === "student")),
    [capabilities]
  );

  const value = useMemo(
    () => ({ user, capabilities, loading, isFaculty, isStudent, getToken, refresh }),
    [user, capabilities, loading, isFaculty, isStudent, getToken, refresh]
  );

  return <ExamAuthContext.Provider value={value}>{children}</ExamAuthContext.Provider>;
}

export function useExamAuth(): ExamAuthContextValue {
  const ctx = useContext(ExamAuthContext);
  if (!ctx) {
    throw new Error("useExamAuth must be used within an ExamAuthProvider");
  }
  return ctx;
}
