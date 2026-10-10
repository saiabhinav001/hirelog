import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ExamShellHeader } from "@/components/exams/ExamShellHeader";

describe("ExamShellHeader", () => {
  it("shows the camera slot as unknown rather than a fabricated reading when no camera prop is given", () => {
    render(<ExamShellHeader connectionHealth="connected" />);
    expect(screen.getByLabelText("Camera status")).toHaveTextContent("Unknown");
  });

  it("shows permission-denied and unsupported honestly, never a fabricated capture", () => {
    const { rerender } = render(<ExamShellHeader connectionHealth="connected" camera={{ permission: "denied", lastCaptureAt: null }} />);
    expect(screen.getByText("Permission denied")).toBeInTheDocument();

    rerender(<ExamShellHeader connectionHealth="connected" camera={{ permission: "unsupported", lastCaptureAt: null }} />);
    expect(screen.getByText("Unsupported")).toBeInTheDocument();
  });

  it("shows capture age once granted and captured", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-06-01T10:00:30Z"));
    render(<ExamShellHeader connectionHealth="connected" camera={{ permission: "granted", lastCaptureAt: "2026-06-01T10:00:00Z" }} />);
    expect(screen.getByText("30s ago")).toBeInTheDocument();
    vi.useRealTimers();
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
    expect(screen.getByText("Draft saved (v3) - not submitted")).toBeInTheDocument();
  });

  it("anchors the timer to the server's time, ignoring the device clock", () => {
    vi.useFakeTimers().setSystemTime(new Date("1999-01-01T00:00:00Z")); // a wrong device clock
    const serverNow = Date.parse("2026-06-01T10:00:00Z");
    render(<ExamShellHeader connectionHealth="connected" deadline="2026-06-01T10:12:05Z" serverNowMs={serverNow} />);
    expect(screen.getByRole("timer")).toHaveTextContent("12:05");
    vi.useRealTimers();
  });

  it("shows zero, never a negative time, once the deadline has passed", () => {
    render(<ExamShellHeader connectionHealth="connected" deadline="2026-06-01T10:00:00Z" serverNowMs={Date.parse("2026-06-01T10:30:00Z")} />);
    expect(screen.getByRole("timer")).toHaveTextContent("00:00");
  });

  it("is not a live region, so a screen reader is not interrupted every second", () => {
    render(<ExamShellHeader connectionHealth="connected" deadline="2026-06-01T10:12:05Z" serverNowMs={Date.parse("2026-06-01T10:00:00Z")} />);
    const timer = screen.getByRole("timer");
    expect(timer).toHaveAttribute("aria-live", "off");
    expect(timer.closest("[aria-live='polite']")).toBeNull();
    expect(screen.getByRole("timer", { name: "Time remaining" })).toBeInTheDocument();
  });
});
