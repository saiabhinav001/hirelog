import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
const replaceMock = vi.fn();
const useAuthMock = vi.fn();
const toastMock = vi.fn();
const getClientAuthTokenMock = vi.fn();
const apiFetchMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: pushMock,
    replace: replaceMock,
  }),
}));

vi.mock("@/context/AuthContext", () => ({
  useAuth: () => useAuthMock(),
}));

vi.mock("@/context/ToastContext", () => ({
  useToast: () => ({ toast: toastMock }),
}));

vi.mock("@/lib/authToken", () => ({
  getClientAuthToken: () => getClientAuthTokenMock(),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

import SubmitPage from "@/app/submit/page";

describe("SubmitPage", () => {
  const refreshProfileMock = vi.fn();

  beforeEach(() => {
    pushMock.mockReset();
    replaceMock.mockReset();
    useAuthMock.mockReset();
    toastMock.mockReset();
    getClientAuthTokenMock.mockReset();
    apiFetchMock.mockReset();
    refreshProfileMock.mockReset();

    useAuthMock.mockReturnValue({
      user: { uid: "user-1" },
      profile: {
        uid: "user-1",
        name: "Ada Lovelace",
        display_name: "Ada",
        email: "ada@example.edu",
        role: "contributor",
      },
      loading: false,
      refreshProfile: refreshProfileMock,
    });

    getClientAuthTokenMock.mockResolvedValue("token");
    apiFetchMock.mockResolvedValue({ id: "exp-1" });
    refreshProfileMock.mockResolvedValue(undefined);
  });

  it("submits experience payload and redirects to contributions", async () => {
    const user = userEvent.setup();

    render(<SubmitPage />);

    await user.type(screen.getByPlaceholderText("e.g. Google"), "OpenAI");
    await user.type(screen.getByPlaceholderText("e.g. SDE Intern"), "Software Intern");
    await user.type(screen.getByPlaceholderText("e.g. Technical Round 1"), "Technical Round 1");
    await user.type(
      screen.getByPlaceholderText("Describe your interview experience..."),
      "Focused on graphs and dynamic programming."
    );
    await user.type(
      screen.getByPlaceholderText(/What is the time complexity of quicksort\?/i),
      "Explain quicksort.\nDesign an LRU cache."
    );

    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalled();
    });

    const submitCall = apiFetchMock.mock.calls.find((call) => call[0] === "/api/experiences");
    expect(submitCall).toBeDefined();

    if (!submitCall) {
      throw new Error("Submit endpoint was not called");
    }

    const request = submitCall[1] as RequestInit;
    const bodyText = typeof request.body === "string" ? request.body : "{}";
    const payload = JSON.parse(bodyText) as {
      company: string;
      role: string;
      user_questions: string[];
      allow_contact: boolean;
    };

    expect(payload.company).toBe("OpenAI");
    expect(payload.role).toBe("Software Intern");
    expect(payload.user_questions).toEqual([
      "Explain quicksort.",
      "Design an LRU cache.",
    ]);
    expect(payload.allow_contact).toBe(false);

    expect(toastMock).toHaveBeenCalled();
    expect(pushMock).toHaveBeenCalledWith("/contributions");
    expect(refreshProfileMock).toHaveBeenCalled();
  });
});
