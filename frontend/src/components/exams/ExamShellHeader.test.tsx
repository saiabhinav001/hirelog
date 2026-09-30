import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ExamShellHeader } from "@/components/exams/ExamShellHeader";

describe("ExamShellHeader", () => {
  it("shows the camera slot as unknown rather than a fabricated reading", () => {
    render(<ExamShellHeader connectionHealth="connected" />);
    expect(screen.getByText("Camera not yet configured")).toBeInTheDocument();
    expect(screen.getByLabelText("Camera status")).toHaveTextContent("Unknown");
  });

  it("labels the delivery profile as standard browser, never a fabricated lockdown claim", () => {
    render(<ExamShellHeader connectionHealth="connected" />);
    expect(screen.getByText("Standard browser")).toBeInTheDocument();
  });

  it("renders each connection health state with a distinct label", () => {
    const { rerender } = render(<ExamShellHeader connectionHealth="connected" />);
    expect(screen.getByText("Connected")).toBeInTheDocument();

    rerender(<ExamShellHeader connectionHealth="reconnecting" />);
    expect(screen.getByText("Reconnecting…")).toBeInTheDocument();

    rerender(<ExamShellHeader connectionHealth="polling" />);
    expect(screen.getByText(/Offline — retrying/)).toBeInTheDocument();
  });

  it("shows a placeholder time when no deadline is known", () => {
    render(<ExamShellHeader connectionHealth="connected" deadline={null} />);
    expect(screen.getByText("--:--")).toBeInTheDocument();
  });

  it("formats a future deadline as mm:ss", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-06-01T10:00:00Z"));
    render(<ExamShellHeader connectionHealth="connected" deadline="2026-06-01T10:05:30Z" />);
    expect(screen.getByText("05:30")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("shows zero warnings by default and the given count when provided", () => {
    const { rerender } = render(<ExamShellHeader connectionHealth="connected" />);
    expect(screen.getByText("0")).toBeInTheDocument();
    rerender(<ExamShellHeader connectionHealth="connected" warningCount={2} />);
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("shows 'Not saved yet' until a save receipt is provided", () => {
    const { rerender } = render(<ExamShellHeader connectionHealth="connected" />);
    expect(screen.getByText("Not saved yet")).toBeInTheDocument();
    rerender(<ExamShellHeader connectionHealth="connected" saveReceipt={{ savedAt: "2026-06-01T10:00:00Z", version: 3 }} />);
    expect(screen.getByText("Saved (v3)")).toBeInTheDocument();
  });
});
