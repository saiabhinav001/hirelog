"use client";

import dynamic from "next/dynamic";

// Lazy-loaded on exam routes only (contract: "lazy-load camera/editor on
// exam routes") — the editor bundle never ships to any other page.
export const LazyCodeEditorPanel = dynamic(() => import("./CodeEditorPanel"), {
  ssr: false,
  loading: () => (
    <div className="card p-4 text-sm text-[var(--text-muted)]" role="status">
      Loading code editor…
    </div>
  ),
});
