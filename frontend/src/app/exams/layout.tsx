import { ExamAuthProvider } from "@/lib/examAuth";

export default function ExamsLayout({ children }: { children: React.ReactNode }) {
  return <ExamAuthProvider>{children}</ExamAuthProvider>;
}
