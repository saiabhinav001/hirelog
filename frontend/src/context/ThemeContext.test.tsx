import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ThemeProvider, useTheme } from "@/context/ThemeContext";

function ThemeProbe() {
  const { theme, resolvedTheme, setTheme } = useTheme();

  return (
    <div>
      <p>theme: {theme}</p>
      <p>resolved: {resolvedTheme}</p>
      <button type="button" onClick={() => setTheme("dark")}>
        Set dark
      </button>
    </div>
  );
}

describe("ThemeProvider", () => {
  it("hydrates from localStorage and applies the stored theme", async () => {
    localStorage.setItem("theme", "dark");

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(document.documentElement.classList.contains("dark")).toBe(true);
    });

    expect(document.documentElement.style.colorScheme).toBe("dark");
    expect(screen.getByText("theme: dark")).toBeInTheDocument();
    expect(screen.getByText("resolved: dark")).toBeInTheDocument();
  });

  it("persists user theme changes", async () => {
    const user = userEvent.setup();

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>
    );

    await user.click(screen.getByRole("button", { name: "Set dark" }));

    await waitFor(() => {
      expect(localStorage.getItem("theme")).toBe("dark");
    });
  });
});
