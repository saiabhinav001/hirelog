import type { ReactNode } from "react";

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
const searchParams = new URLSearchParams("q=graphs&limit=20");

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: pushMock,
  }),
  useSearchParams: () => searchParams,
}));

vi.mock("@/components/Motion", () => ({
  FadeIn: ({ children }: { children: ReactNode }) => <>{children}</>,
  StaggerContainer: ({ children, className }: { children: ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
  StaggerItem: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "@/lib/api";
import ResultsPage from "@/app/results/page";

const apiFetchMock = vi.mocked(apiFetch);

describe("ResultsPage", () => {
  beforeEach(() => {
    pushMock.mockReset();
    apiFetchMock.mockReset();
  });

  it("renders search results with trust metadata", async () => {
    apiFetchMock.mockResolvedValue({
      results: [
        {
          id: "exp-1",
          company: "OpenAI",
          role: "Intern",
          year: 2024,
          round: "Technical Round 1",
          difficulty: "Medium",
          raw_text: "Round notes",
          extracted_questions: [
            {
              question_text: "What is memoization?",
              question: "What is memoization?",
              topic: "DP",
              category: "Algorithms",
              confidence: 1,
              source: "user",
            },
            {
              question_text: "Explain BFS complexity.",
              question: "Explain BFS complexity.",
              topic: "Graphs",
              category: "Algorithms",
              confidence: 0.82,
              source: "ai",
            },
          ],
          topics: ["Graphs"],
          summary: "Graph-heavy process with one coding and one system question.",
          created_by: "user-1",
          nlp_status: "done",
          stats: {
            user_question_count: 1,
            extracted_question_count: 1,
            total_question_count: 2,
          },
        },
      ],
      total: 1,
      total_count: 1,
      returned_count: 1,
      has_more: false,
      next_cursor: null,
      served_mode: "hybrid",
      served_engine: "faiss",
    });

    render(<ResultsPage />);

    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
    expect(screen.getByText("Served via hybrid on faiss")).toBeInTheDocument();
    expect(screen.getByText("AI done")).toBeInTheDocument();
    expect(screen.getByText("user")).toBeInTheDocument();
    expect(screen.getByText("ai")).toBeInTheDocument();
    expect(screen.getByText("confidence 82%")).toBeInTheDocument();
  });
});
