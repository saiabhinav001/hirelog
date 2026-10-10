"use client";

export type NavigatorSlot = { slot_index: number; answered: boolean; marked_for_review: boolean };

type Props = {
  totalSlots: number;
  slots: NavigatorSlot[]; // released slots only
  viewIndex: number;
  releasedIndex: number;
  revisitEnabled: boolean;
  disabled?: boolean;
  onSelect: (slotIndex: number) => void;
};

export function navigatorCounts(totalSlots: number, slots: NavigatorSlot[]) {
  const answered = slots.filter((s) => s.answered).length;
  return {
    answered,
    unanswered: slots.length - answered,
    marked: slots.filter((s) => s.marked_for_review).length,
    locked: Math.max(0, totalSlots - slots.length),
  };
}

function label(slot: NavigatorSlot | undefined, index: number, selectable: boolean, isView: boolean): string {
  if (!slot) return `Question ${index + 1}, locked`;
  const parts = [`Question ${index + 1}`, slot.answered ? "answered" : "not answered"];
  if (slot.marked_for_review) parts.push("marked for review");
  if (isView) parts.push("current view");
  if (!selectable) parts.push("review not available");
  return parts.join(", ");
}

/** Numbered question map with counts. Locked questions are inert; the server decides what is released. */
export function QuestionNavigator({ totalSlots, slots, viewIndex, releasedIndex, revisitEnabled, disabled = false, onSelect }: Props) {
  const counts = navigatorCounts(totalSlots, slots);
  const bySlot = new Map(slots.map((s) => [s.slot_index, s]));

  return (
    <nav aria-label="Question navigator" className="card p-4">
      <h2 className="text-sm font-semibold">Questions</h2>
      <p className="mt-1 text-xs text-[var(--text-secondary)]" data-testid="navigator-counts">
        Answered {counts.answered} · Unanswered {counts.unanswered} · Marked for review {counts.marked} · Locked {counts.locked}
      </p>
      <ol className="mt-3 grid grid-cols-5 gap-2">
        {Array.from({ length: totalSlots }, (_, index) => {
          const slot = bySlot.get(index);
          const released = index <= releasedIndex && slot !== undefined;
          const selectable = released && !disabled && (revisitEnabled || index === releasedIndex);
          const isView = index === viewIndex;
          return (
            <li key={index}>
              <button
                type="button"
                aria-label={label(slot, index, selectable || !released, isView)}
                aria-current={isView ? "step" : undefined}
                disabled={!selectable}
                onClick={() => onSelect(index)}
                className={[
                  "relative h-10 w-full rounded-md border text-sm font-medium",
                  isView ? "border-[var(--primary)] bg-[var(--primary-soft)] text-[var(--text)]" : "border-[var(--border-hover)] text-[var(--text)]",
                  !released ? "bg-[var(--surface-muted)] text-[var(--text-muted)]" : "",
                  slot?.answered ? "font-semibold" : "",
                  "disabled:cursor-not-allowed disabled:opacity-80",
                ].join(" ")}
              >
                {index + 1}
                {slot?.answered && <span aria-hidden="true" className="absolute bottom-0.5 right-1 text-[0.6rem]">✓</span>}
                {slot?.marked_for_review && <span aria-hidden="true" className="absolute left-1 top-0.5 text-[0.6rem]">⚑</span>}
                {!released && <span aria-hidden="true" className="absolute bottom-0.5 right-1 text-[0.6rem]">–</span>}
              </button>
            </li>
          );
        })}
      </ol>
      <p className="mt-3 text-xs text-[var(--text-muted)]">
        ✓ answered · ⚑ marked for review · – locked
        {!revisitEnabled && releasedIndex > 0 ? " · Earlier questions can't be reopened in this exam." : ""}
      </p>
    </nav>
  );
}
