import type { ReactNode } from "react";

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const useAuthMock = vi.fn();
const getClientAuthTokenMock = vi.fn();
const apiFetchMock = vi.fn();

vi.mock("@/components/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/context/AuthContext", () => ({
  useAuth: () => useAuthMock(),
}));

vi.mock("@/lib/authToken", () => ({
  getClientAuthToken: () => getClientAuthTokenMock(),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

import DashboardPage from "@/app/dashboard/page";

function buildAuthMock() {
  return {
    user: { uid: "user-1" },
    profile: {
      uid: "user-1",
      id: "user-1",
      name: "Student",
      email: "student@example.edu",
      role: "viewer" as const,
    },
    loading: false,
    signIn: vi.fn(),
    signUp: vi.fn(),
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
    refreshProfile: vi.fn(),
  };
}

describe("DashboardPage fallbacks", () => {
  beforeEach(() => {
    useAuthMock.mockReset();
    getClientAuthTokenMock.mockReset();
    apiFetchMock.mockReset();

    useAuthMock.mockReturnValue(buildAuthMock());
    getClientAuthTokenMock.mockResolvedValue("token");
  });

  it("shows no-data fallback when archive is empty", async () => {
    apiFetchMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/dashboard/stats")) {
        return Promise.resolve({
          total_experiences: 0,
          top_company: null,
          top_topic: null,
          contribution_impact: {
            experiences_submitted: 0,
            questions_extracted: 0,
            archive_size: 0,
          },
        });
      }

      if (path.startsWith("/api/dashboard/charts")) {
        return Promise.resolve({
          topic_totals: {},
          difficulty_distribution: {},
          company_topic_counts: {},
          insights: [],
        });
      }

      if (path.startsWith("/api/dashboard/questions")) {
        return Promise.resolve({ frequent_questions: {} });
      }

      if (path.startsWith("/api/dashboard/flows")) {
        return Promise.resolve({ interview_progression: {} });
      }

      return Promise.resolve({});
    });

    render(<DashboardPage />);

    expect(await screen.findByText("No data yet")).toBeInTheDocument();
    expect(
      screen.getByText("The archive needs interview experiences to generate placement analytics.")
    ).toBeInTheDocument();
  });

  it("shows retry fallback when stats request fails", async () => {
    apiFetchMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/dashboard/stats")) {
        return Promise.reject(new Error("stats failure"));
      }

      if (path.startsWith("/api/dashboard/charts")) {
        return Promise.resolve({
          topic_totals: {},
          difficulty_distribution: {},
          company_topic_counts: {},
          insights: [],
        });
      }

      if (path.startsWith("/api/dashboard/questions")) {
        return Promise.resolve({ frequent_questions: {} });
      }

      if (path.startsWith("/api/dashboard/flows")) {
        return Promise.resolve({ interview_progression: {} });
      }

      return Promise.resolve({});
    });

    render(<DashboardPage />);

    expect(await screen.findByText("Couldn't load analytics. Please try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
