import type { Metadata } from "next";

import { ExamServiceWorkerRegistrar } from "@/components/exams/ExamServiceWorkerRegistrar";
import { ExamAuthProvider } from "@/lib/examAuth";

export const metadata: Metadata = {
  manifest: "/exams-manifest.webmanifest",
};

export default function ExamsLayout({ children }: { children: React.ReactNode }) {
  return (
    <ExamAuthProvider>
      <ExamServiceWorkerRegistrar />
      <div className="exam-scope contents">{children}</div>
    </ExamAuthProvider>
  );
}
