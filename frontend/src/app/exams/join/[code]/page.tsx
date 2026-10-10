"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { ExamApiError } from "@/lib/examsApi";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints } from "@/lib/examEndpoints";

/** Opens an exam from a short code. The code only names the run; the preflight and admission checks that follow decide everything else. */
function JoinContent() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const { getToken } = useExamAuth();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { run_id } = await examEndpoints.resolveShortLink(await getToken(), code);
        if (!cancelled) router.replace(`/exams/student/${run_id}/preflight`);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ExamApiError && err.status === 429
            ? "Too many attempts. Wait a moment and open the link again."
            : "This link isn't valid for your account, or it has been withdrawn. Ask your teacher for a new one."
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, getToken, router]);

  if (error) return <ErrorState title="Can't open this exam link" description={error} />;
  return <LoadingState message="Finding your exam…" />;
}

export default function JoinPage() {
  return (
    <ExamProtectedRoute requireRole="student">
      <JoinContent />
    </ExamProtectedRoute>
  );
}
