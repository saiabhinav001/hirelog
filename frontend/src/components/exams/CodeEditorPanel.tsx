"use client";

/**
 * Plain, accessible textarea-based code editor. This IS the fallback — no
 * rich editor (Monaco/CodeMirror) is wired in this phase, so there is
 * nothing more elaborate to "fall back" from; this keeps the exam's most
 * safety/accessibility-sensitive input reliable and fully keyboard/screen-
 * reader operable everywhere, rather than depending on an unvetted heavy
 * dependency. Loaded via `next/dynamic` so it's lazy-loaded on exam routes.
 */

export function CodeEditorPanel({
  value,
  onChange,
  language,
  readOnly,
}: {
  value: string;
  onChange: (next: string) => void;
  language: string;
  readOnly?: boolean;
}) {
  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const target = event.currentTarget;
      const start = target.selectionStart;
      const end = target.selectionEnd;
      const next = `${value.slice(0, start)}    ${value.slice(end)}`;
      onChange(next);
      requestAnimationFrame(() => {
        target.selectionStart = target.selectionEnd = start + 4;
      });
    }
  };

  return (
    <div className="card p-0 overflow-hidden">
      <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2">
        <label htmlFor="code-editor-textarea" className="label mb-0">
          Code ({language})
        </label>
        <span className="text-xs text-[var(--text-muted)]">{value.split("\n").length} lines</span>
      </div>
      <textarea
        id="code-editor-textarea"
        aria-label={`Code editor for ${language}`}
        className="input-field w-full rounded-none border-0 font-mono text-sm leading-6"
        style={{ minHeight: 320, resize: "vertical" }}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        readOnly={readOnly}
      />
    </div>
  );
}

export default CodeEditorPanel;
