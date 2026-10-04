"use client";

/**
 * Syntax-highlighted code editor (CodeMirror 6), loaded via `next/dynamic`
 * so the editor bundle only ever ships to exam routes.
 *
 * Tab is intentionally NEVER bound to indentation here (Prompt 10: "do not
 * disable browser shortcuts in ways that break accessibility") — CodeMirror
 * only traps Tab for indentation if the consumer explicitly adds the
 * `indentWithTab` keymap; we deliberately never import or add it, so Tab
 * always moves focus to the next control instead of inserting a tab
 * character (WCAG 2.1.2 — avoids a keyboard trap for keyboard-only and
 * screen-reader users).
 *
 * Copy/cut/paste are blocked on the editor surface (exam integrity
 * requirement) — each blocked attempt is reported once via
 * `onClipboardBlocked` so it can be logged as a proctoring observation,
 * never silently swallowed.
 */

import { useEffect, useRef } from "react";

import { cpp } from "@codemirror/lang-cpp";
import { java } from "@codemirror/lang-java";
import { python } from "@codemirror/lang-python";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { oneDark } from "@codemirror/theme-one-dark";
import { EditorView } from "@codemirror/view";
import { basicSetup } from "codemirror";

function languageExtension(language: string): Extension {
  if (language.startsWith("python")) return python();
  if (language.startsWith("java")) return java();
  if (language.startsWith("cpp")) return cpp();
  return [];
}

function readOnlyExtension(readOnly: boolean): Extension {
  return [EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)];
}

// CodeMirror auto-sizes its height to the document's content by default —
// without this, the editor rendered as a single ~28px line and the rest of
// the intended editor area was just blank space (found live: a dark
// one-dark background existed but was barely visible at 1 line tall).
const sizeTheme = EditorView.theme({
  "&": { height: "320px" },
  ".cm-scroller": { overflow: "auto" },
});

export function CodeEditorPanel({
  value,
  onChange,
  language,
  readOnly,
  onClipboardBlocked,
}: {
  value: string;
  onChange: (next: string) => void;
  language: string;
  readOnly?: boolean;
  onClipboardBlocked?: (action: "copy" | "cut" | "paste") => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const languageCompartment = useRef(new Compartment()).current;
  const readOnlyCompartment = useRef(new Compartment()).current;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const onClipboardBlockedRef = useRef(onClipboardBlocked);
  onClipboardBlockedRef.current = onClipboardBlocked;

  // Created once on mount — language/readOnly/value are all updated through
  // the refs/compartments below rather than tearing the view down, so
  // cursor position, undo history and scroll position survive prop changes.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;

    const blockClipboardAction = (action: "copy" | "cut" | "paste") => (event: ClipboardEvent) => {
      event.preventDefault();
      onClipboardBlockedRef.current?.(action);
    };

    const view = new EditorView({
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          languageCompartment.of(languageExtension(language)),
          readOnlyCompartment.of(readOnlyExtension(Boolean(readOnly))),
          oneDark,
          sizeTheme,
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": `Code editor for ${language}` }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              onChangeRef.current(update.state.doc.toString());
            }
          }),
          EditorView.domEventHandlers({
            copy: blockClipboardAction("copy"),
            cut: blockClipboardAction("cut"),
            paste: blockClipboardAction("paste"),
          }),
        ],
      }),
      parent: host,
    });
    viewRef.current = view;

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Intentionally mount-only — see compartment/ref effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // External value changes (e.g. loading a saved draft) — only replace the
  // document when it actually differs, otherwise every keystroke's own
  // onChange round-trip would reset the cursor to the start.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
    }
  }, [value]);

  useEffect(() => {
    viewRef.current?.dispatch({ effects: languageCompartment.reconfigure(languageExtension(language)) });
  }, [language, languageCompartment]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: readOnlyCompartment.reconfigure(readOnlyExtension(Boolean(readOnly))),
    });
  }, [readOnly, readOnlyCompartment]);

  return (
    <div className="card p-0 overflow-hidden">
      <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2">
        <span className="label mb-0">Code ({language})</span>
        <span className="text-xs text-[var(--text-muted)]">{value.split("\n").length} lines</span>
      </div>
      <div ref={hostRef} style={{ minHeight: 320 }} className="text-sm [&_.cm-editor]:h-full [&_.cm-scroller]:font-mono" />
    </div>
  );
}

export default CodeEditorPanel;
