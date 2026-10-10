"use client";

import { useEffect, useId, useRef, useState } from "react";

type Props = {
  title: string;
  details?: { label: string; value: string }[];
  consequence?: string;
  reasonLabel?: string;
  reasonRequired?: boolean;
  numberLabel?: string;
  numberDefault?: number;
  numberMax?: number;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: (reason: string, amount: number) => void;
  onCancel: () => void;
};

/**
 * Confirmation for actions that change a run or a student's attempt. Focus starts on Cancel, Escape cancels, Tab stays inside,
 * and the consequence is spelled out before anything is sent.
 */
export function ConfirmDialog({ title, details = [], consequence, reasonLabel, reasonRequired = false, numberLabel, numberDefault = 10, numberMax, confirmLabel, danger = false, busy = false, onConfirm, onCancel }: Props) {
  const titleId = useId();
  const reasonId = useId();
  const [reason, setReason] = useState("");
  const [amount, setAmount] = useState(numberDefault);
  const amountId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    opener.current = document.activeElement;
    cancelRef.current?.focus();
    return () => {
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
  }, []);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape" && !busy) {
      event.stopPropagation();
      onCancel();
      return;
    }
    if (event.key !== "Tab" || !rootRef.current) return;
    const focusable = rootRef.current.querySelectorAll<HTMLElement>("button:not(:disabled), textarea, input, [href]");
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const missingReason = reasonRequired && reason.trim().length < 3;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onKeyDown={onKeyDown}>
      <div ref={rootRef} role="dialog" aria-modal="true" aria-labelledby={titleId} className="card w-full max-w-md p-6" style={danger ? { borderColor: "var(--error-border)" } : undefined}>
        <h2 id={titleId} className="text-lg font-semibold">{title}</h2>
        {details.length > 0 && (
          <dl className="mt-3 space-y-1 text-sm">
            {details.map((d) => (
              <div key={d.label} className="flex justify-between gap-4">
                <dt className="text-[var(--text-muted)]">{d.label}</dt>
                <dd className="text-right">{d.value}</dd>
              </div>
            ))}
          </dl>
        )}
        {consequence && <p className="mt-3 text-sm text-[var(--text-secondary)]">{consequence}</p>}
        {numberLabel && (
          <div className="mt-3">
            <label htmlFor={amountId} className="label">{numberLabel}</label>
            <input id={amountId} type="number" min={1} max={numberMax} className="input-field w-28" value={amount} onChange={(e) => setAmount(Number(e.target.value))} />
          </div>
        )}
        {reasonLabel && (
          <div className="mt-3">
            <label htmlFor={reasonId} className="label">{reasonLabel}{reasonRequired ? " (required, audited)" : " (optional, audited)"}</label>
            <textarea id={reasonId} className="input-field w-full" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
        )}
        <div className="mt-5 flex justify-end gap-3">
          <button ref={cancelRef} type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="button" className="btn-primary" onClick={() => onConfirm(reason.trim(), amount)} disabled={busy || missingReason || (Boolean(numberLabel) && !(amount >= 1))}>
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
