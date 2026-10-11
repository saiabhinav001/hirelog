import type { Metadata } from "next";

import { ExamServiceWorkerRegistrar } from "@/components/exams/ExamServiceWorkerRegistrar";
import { ExamsStagingBar } from "@/components/exams/ExamsStagingBar";
import { ExamAuthProvider } from "@/lib/examAuth";
import { EXAMS_STAGING } from "@/lib/deployment";

export const metadata: Metadata = {
  manifest: "/exams-manifest.webmanifest",
};

export default function ExamsLayout({ children }: { children: React.ReactNode }) {
  return (
    <ExamAuthProvider>
      <ExamServiceWorkerRegistrar />
      {EXAMS_STAGING && <ExamsStagingBar />}
      {children}
    </ExamAuthProvider>
  );
}
