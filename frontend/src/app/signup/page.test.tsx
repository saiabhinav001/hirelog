import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
const useAuthMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: pushMock,
  }),
}));

vi.mock("@/context/AuthContext", () => ({
  useAuth: () => useAuthMock(),
}));

import SignupPage from "@/app/signup/page";

describe("SignupPage", () => {
  const signUpMock = vi.fn();
  const signInWithGoogleMock = vi.fn();

  beforeEach(() => {
    pushMock.mockReset();
    signUpMock.mockReset();
    signInWithGoogleMock.mockReset();

    useAuthMock.mockReturnValue({
      signUp: signUpMock,
      signInWithGoogle: signInWithGoogleMock,
    });
  });

  it("submits credentials and redirects to search", async () => {
    const user = userEvent.setup();
    signUpMock.mockResolvedValue(undefined);

    render(<SignupPage />);

    await user.type(screen.getByLabelText("Full name"), "Ada Lovelace");
    await user.type(screen.getByLabelText("Email address"), "ada@example.edu");
    await user.type(screen.getByLabelText("Password"), "pass1234");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(signUpMock).toHaveBeenCalledWith("Ada Lovelace", "ada@example.edu", "pass1234");
    });

    expect(pushMock).toHaveBeenCalledWith("/search");
  });

  it("shows backend errors when signup fails", async () => {
    const user = userEvent.setup();
    signUpMock.mockRejectedValue(new Error("Email already in use"));

    render(<SignupPage />);

    await user.type(screen.getByLabelText("Full name"), "Ada Lovelace");
    await user.type(screen.getByLabelText("Email address"), "ada@example.edu");
    await user.type(screen.getByLabelText("Password"), "pass1234");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText("Email already in use")).toBeInTheDocument();
  });
});
