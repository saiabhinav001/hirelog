import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "@/components/exams/ConfirmDialog";

function setup(props: Partial<React.ComponentProps<typeof ConfirmDialog>> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(<ConfirmDialog title="End attempt?" confirmLabel="End attempt" onConfirm={onConfirm} onCancel={onCancel} {...props} />);
  return { onConfirm, onCancel };
}

describe("ConfirmDialog", () => {
  it("is a labelled modal dialog that starts on Cancel, so Enter cannot confirm by accident", () => {
    setup();
    expect(screen.getByRole("dialog", { name: "End attempt?" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("Escape cancels", async () => {
    const { onCancel, onConfirm } = setup();
    await userEvent.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("keeps Tab inside the dialog", async () => {
    setup({ reasonLabel: "Reason" });
    await userEvent.tab();
    await userEvent.tab();
    await userEvent.tab();
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });

  it("will not confirm without a required reason, and passes the trimmed reason", async () => {
    const { onConfirm } = setup({ reasonLabel: "Reason", reasonRequired: true });
    const confirm = screen.getByRole("button", { name: "End attempt" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Reason/), "  cheating  ");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("cheating", 10);
  });

  it("collects a number when asked", async () => {
    const { onConfirm } = setup({ numberLabel: "Extra minutes", numberDefault: 15, confirmLabel: "Extend" });
    const input = screen.getByLabelText("Extra minutes");
    expect(input).toHaveValue(15);
    await userEvent.clear(input);
    await userEvent.type(input, "20");
    await userEvent.click(screen.getByRole("button", { name: "Extend" }));
    expect(onConfirm).toHaveBeenCalledWith("", 20);
  });

  it("shows the consequence and the named details", () => {
    setup({ consequence: "It cannot be reopened.", details: [{ label: "Student", value: "u-42" }] });
    expect(screen.getByText("It cannot be reopened.")).toBeInTheDocument();
    expect(screen.getByText("u-42")).toBeInTheDocument();
  });

  it("disables both buttons while busy", () => {
    setup({ busy: true });
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
  });
});
