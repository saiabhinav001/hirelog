"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useExamAuth } from "@/lib/examAuth";
import { LoadingState } from "@/components/States";

const EXAMS_ENABLED = process.env.NEXT_PUBLIC_EXAMS_ENABLED === "true";

export function ExamProtectedRoute({
  children,
  requireRole,
}: {
  children: React.ReactNode;
  requireRole?: "faculty" | "student";
}) {
  const router = useRouter();
  const { user, loading, isFaculty, isStudent } = useExamAuth();

  useEffect(() => {
    if (!loading && !user) {
      router.replace("/exams/login");
    }
  }, [loading, router, user]);

  if (!EXAMS_ENABLED) {
    return <p className="page-container py-12 text-sm text-[var(--text-muted)]">Exams module is not enabled.</p>;
  }
  if (loading) {
    return <LoadingState message="Checking your exam access…" />;
  }
  if (!user) {
    return null;
  }
  if (requireRole === "faculty" && !isFaculty) {
    return (
      <div className="page-container py-12">
        <div className="card p-8 text-center max-w-md mx-auto">
          <h3 className="text-lg font-semibold">Faculty access required</h3>
          <p className="mt-2 text-sm text-[var(--text-muted)]">
            Your account does not have exam faculty authority.
          </p>
        </div>
      </div>
    );
  }
  if (requireRole === "student" && !isStudent) {
    return (
      <div className="page-container py-12">
        <div className="card p-8 text-center max-w-md mx-auto">
          <h3 className="text-lg font-semibold">Student enrollment required</h3>
          <p className="mt-2 text-sm text-[var(--text-muted)]">
            No active student enrollment was found for your account.
          </p>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
