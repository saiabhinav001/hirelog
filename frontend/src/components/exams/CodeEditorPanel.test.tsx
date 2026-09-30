import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CodeEditorPanel } from "@/components/exams/CodeEditorPanel";

describe("CodeEditorPanel", () => {
  it("is a real accessible textarea, not a rich-editor stub", () => {
    render(<CodeEditorPanel value="" onChange={vi.fn()} language="python3.11" />);
    const textarea = screen.getByLabelText("Code editor for python3.11");
    expect(textarea.tagName).toBe("TEXTAREA");
  });

  it("reports line count", () => {
    render(<CodeEditorPanel value={"line one\nline two\nline three"} onChange={vi.fn()} language="python3.11" />);
    expect(screen.getByText("3 lines")).toBeInTheDocument();
  });

  it("calls onChange as the user types", async () => {
    const onChange = vi.fn();
    render(<CodeEditorPanel value="" onChange={onChange} language="python3.11" />);
    const textarea = screen.getByLabelText("Code editor for python3.11");
    await userEvent.type(textarea, "x");
    expect(onChange).toHaveBeenCalledWith("x");
  });

  it("respects readOnly", () => {
    render(<CodeEditorPanel value="locked" onChange={vi.fn()} language="python3.11" readOnly />);
    expect(screen.getByLabelText("Code editor for python3.11")).toHaveAttribute("readonly");
  });
});
