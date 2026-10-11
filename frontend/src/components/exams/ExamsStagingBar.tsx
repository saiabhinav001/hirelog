"use client";

import { signOut } from "firebase/auth";
import { useRouter } from "next/navigation";

import { useExamAuth } from "@/lib/examAuth";
import { auth } from "@/lib/firebase";

/**
 * Header of the exams-only (staging) deployment. It replaces the legacy HireLog
 * navigation, whose links lead to features this deployment does not serve.
 */
export function ExamsStagingBar() {
  const router = useRouter();
  const { user } = useExamAuth();

  const handleSignOut = async () => {
    await signOut(auth);
    router.replace("/exams/login");
  };

  return (
    <header className="border-b border-[var(--border)]">
      <div className="page-container flex items-center justify-between py-3">
        <span className="font-semibold">
          HireLog Exams <span className="badge ml-2">Staging</span>
        </span>
        {user && (
          <button type="button" className="btn-secondary" onClick={() => void handleSignOut()}>
            Sign out
          </button>
        )}
      </div>
    </header>
  );
}
