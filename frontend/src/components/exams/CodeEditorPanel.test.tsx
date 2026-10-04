import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CodeEditorPanel } from "@/components/exams/CodeEditorPanel";

describe("CodeEditorPanel", () => {
  it("renders a syntax-highlighting editor with an accessible name", () => {
    render(<CodeEditorPanel value="" onChange={vi.fn()} language="python3.11" />);
    expect(screen.getByLabelText("Code editor for python3.11")).toBeInTheDocument();
  });

  it("reports line count", () => {
    render(<CodeEditorPanel value={"line one\nline two\nline three"} onChange={vi.fn()} language="python3.11" />);
    expect(screen.getByText("3 lines")).toBeInTheDocument();
  });

  it("respects readOnly", () => {
    render(<CodeEditorPanel value="locked" onChange={vi.fn()} language="python3.11" readOnly />);
    expect(screen.getByLabelText("Code editor for python3.11")).toHaveAttribute("contenteditable", "false");
  });

  it("never traps Tab focus inside the editor (WCAG 2.1.2 no keyboard trap)", async () => {
    render(
      <div>
        <CodeEditorPanel value="" onChange={vi.fn()} language="python3.11" />
        <button>Next control</button>
      </div>
    );
    const editor = screen.getByLabelText("Code editor for python3.11");
    editor.focus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Next control" })).toHaveFocus();
  });

  it("blocks copy/cut/paste and reports each attempt", () => {
    const onClipboardBlocked = vi.fn();
    render(
      <CodeEditorPanel value="secret" onChange={vi.fn()} language="python3.11" onClipboardBlocked={onClipboardBlocked} />
    );
    const editor = screen.getByLabelText("Code editor for python3.11");

    const copyEvent = new Event("copy", { bubbles: true, cancelable: true });
    const cutEvent = new Event("cut", { bubbles: true, cancelable: true });
    const pasteEvent = new Event("paste", { bubbles: true, cancelable: true });

    editor.dispatchEvent(copyEvent);
    editor.dispatchEvent(cutEvent);
    editor.dispatchEvent(pasteEvent);

    expect(copyEvent.defaultPrevented).toBe(true);
    expect(cutEvent.defaultPrevented).toBe(true);
    expect(pasteEvent.defaultPrevented).toBe(true);
    expect(onClipboardBlocked).toHaveBeenNthCalledWith(1, "copy");
    expect(onClipboardBlocked).toHaveBeenNthCalledWith(2, "cut");
    expect(onClipboardBlocked).toHaveBeenNthCalledWith(3, "paste");
  });
});
