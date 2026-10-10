import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { navigatorCounts, QuestionNavigator, type NavigatorSlot } from "@/components/exams/QuestionNavigator";

const slots: NavigatorSlot[] = [
  { slot_index: 0, answered: true, marked_for_review: false },
  { slot_index: 1, answered: false, marked_for_review: true },
  { slot_index: 2, answered: true, marked_for_review: true },
];

function renderNav(overrides: Partial<React.ComponentProps<typeof QuestionNavigator>> = {}) {
  const onSelect = vi.fn();
  render(
    <QuestionNavigator totalSlots={6} slots={slots} viewIndex={2} releasedIndex={2} revisitEnabled onSelect={onSelect} {...overrides} />
  );
  return onSelect;
}

describe("QuestionNavigator", () => {
  it("counts answered, unanswered, marked and locked questions", () => {
    expect(navigatorCounts(6, slots)).toEqual({ answered: 2, unanswered: 1, marked: 2, locked: 3 });
    renderNav();
    expect(screen.getByTestId("navigator-counts")).toHaveTextContent("Answered 2 · Unanswered 1 · Marked for review 2 · Locked 3");
  });

  it("labels every question with its state in words, not colour alone", () => {
    renderNav();
    expect(screen.getByRole("button", { name: "Question 1, answered" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Question 2, not answered, marked for review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Question 3, answered, marked for review, current view" })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("button", { name: "Question 4, locked" })).toBeDisabled();
  });

  it("never lets a locked question be selected", async () => {
    const onSelect = renderNav();
    await userEvent.click(screen.getByRole("button", { name: "Question 5, locked" }));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("selects an earlier released question when reopening is allowed", async () => {
    const onSelect = renderNav();
    await userEvent.click(screen.getByRole("button", { name: "Question 1, answered" }));
    expect(onSelect).toHaveBeenCalledWith(0);
  });

  it("is operable from the keyboard", async () => {
    const onSelect = renderNav();
    screen.getByRole("button", { name: "Question 1, answered" }).focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.tab();
    await userEvent.keyboard(" ");
    expect(onSelect.mock.calls.map((c) => c[0])).toEqual([0, 1]);
  });

  it("makes earlier questions inert, and says why, when the exam does not allow reopening them", async () => {
    const onSelect = renderNav({ revisitEnabled: false });
    const earlier = screen.getByRole("button", { name: /Question 1, answered, review not available/ });
    expect(earlier).toBeDisabled();
    await userEvent.click(earlier);
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByText(/Earlier questions can't be reopened/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Question 3, .*current view/ })).toBeEnabled();
  });

  it("disables every question while time is up", () => {
    renderNav({ disabled: true });
    expect(screen.getAllByRole("button").every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
  });

  it("exposes a labelled navigation landmark with a list of every question", () => {
    renderNav();
    expect(screen.getByRole("navigation", { name: "Question navigator" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(6);
  });
});
