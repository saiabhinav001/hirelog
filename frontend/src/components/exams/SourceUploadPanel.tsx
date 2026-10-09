"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { examEndpoints, type KtUpload, type KtUploadDetail, type KtUploadStatus } from "@/lib/examEndpoints";

const ACTIVE: KtUploadStatus[] = ["quarantined", "scanning", "clean", "parsing"];
const POLL_MS = 3000;
const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";

const STATUS_LABEL: Record<KtUploadStatus, string> = {
  quarantined: "Queued",
  scanning: "Scanning for malware",
  clean: "Scanned",
  parsing: "Reading the document",
  parsed: "Ready",
  parse_failed: "Could not be read",
  scan_unavailable: "Not scanned",
  infected: "Blocked by malware scan",
  rejected: "Rejected",
  deleted: "Deleted",
};

function badgeClass(status: KtUploadStatus): string {
  if (status === "parsed") return "badge-success";
  if (["parse_failed", "infected", "rejected"].includes(status)) return "badge-error";
  return "badge-warning";
}

// Bytes are gone or untrustworthy for these: only a new upload (replace) can help.
const NOT_RETRYABLE = new Set(["storage_missing", "integrity_mismatch"]);

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

type Props = {
  getToken: () => Promise<string | undefined>;
  onSourcesChanged?: () => void;
};

export function SourceUploadPanel({ getToken, onSourcesChanged }: Props) {
  const [uploads, setUploads] = useState<KtUpload[] | null>(null);
  const [progress, setProgress] = useState<Record<string, number>>({});
  const [errors, setErrors] = useState<string[]>([]);
  const [details, setDetails] = useState<Record<string, KtUploadDetail>>({});
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const mounted = useRef(true);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.listKtUploads(token);
      if (mounted.current) setUploads(data.results);
    } catch (err) {
      if (mounted.current) setErrors((prev) => [...prev, err instanceof Error ? err.message : "Could not load uploads."]);
    }
  }, [getToken]);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const hasActive = uploads?.some((u) => ACTIVE.includes(u.status)) ?? false;
  useEffect(() => {
    if (!hasActive) return;
    const timer = setTimeout(load, POLL_MS);
    return () => clearTimeout(timer);
  }, [hasActive, uploads, load]);

  const fail = (message: string) => setErrors((prev) => [...prev, message]);

  const sendFiles = async (files: File[], replaceId?: string) => {
    setErrors([]);
    for (const file of files) {
      setProgress((prev) => ({ ...prev, [file.name]: 0 }));
      try {
        const token = await getToken();
        const onProgress = (fraction: number) => setProgress((prev) => ({ ...prev, [file.name]: fraction }));
        if (replaceId) await examEndpoints.replaceKtUpload(token, replaceId, file, onProgress);
        else await examEndpoints.uploadKtFile(token, file, onProgress);
      } catch (err) {
        fail(`${file.name}: ${err instanceof Error ? err.message : "upload failed"}`);
      } finally {
        setProgress((prev) => {
          const { [file.name]: _done, ...rest } = prev;
          void _done;
          return rest;
        });
      }
    }
    await load();
  };

  const act = async (id: string, action: () => Promise<unknown>) => {
    setBusyId(id);
    setErrors([]);
    try {
      await action();
    } catch (err) {
      fail(err instanceof Error ? err.message : "That action failed.");
    } finally {
      setBusyId(null);
      await load();
    }
  };

  const toggleDetails = async (id: string) => {
    if (details[id]) {
      setDetails((prev) => {
        const { [id]: _removed, ...rest } = prev;
        void _removed;
        return rest;
      });
      return;
    }
    try {
      const token = await getToken();
      const detail = await examEndpoints.getKtUpload(token, id);
      setDetails((prev) => ({ ...prev, [id]: detail }));
    } catch (err) {
      fail(err instanceof Error ? err.message : "Could not load details.");
    }
  };

  const visible = (uploads ?? []).filter((u) => u.status !== "deleted");

  return (
    <div className="mt-4" aria-live="polite">
      <label className="block text-sm font-medium" htmlFor="kt-upload-input">
        Upload documents (PDF, DOCX or TXT, up to 25 MB each)
      </label>
      <input
        id="kt-upload-input"
        className="input-field mt-1 w-full"
        type="file"
        multiple
        accept={ACCEPT}
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          if (files.length) void sendFiles(files);
        }}
      />
      <p className="mt-1 text-xs text-[var(--text-muted)]">
        Files are stored privately and scanned before they are read. Scanned (image-only) pages, tables and images are not
        read — you will see exactly what was skipped.
      </p>

      {Object.entries(progress).map(([name, fraction]) => (
        <div key={name} className="mt-2 text-sm">
          <span>{name}</span>
          <progress className="ml-2 align-middle" max={1} value={fraction} aria-label={`Uploading ${name}`} />
          <span className="ml-2">{Math.round(fraction * 100)}%</span>
        </div>
      ))}

      {errors.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm text-[var(--error)]" role="alert">
          {errors.map((message, index) => (
            <li key={`${index}-${message}`}>{message}</li>
          ))}
        </ul>
      )}

      <div className="mt-4 space-y-2">
        {uploads && visible.length === 0 && <p className="text-sm text-[var(--text-muted)]">No uploaded documents yet.</p>}
        {visible.map((u) => {
          const detail = details[u.id];
          const warnings = detail?.versions.at(-1)?.warnings ?? [];
          const canRetry = (u.status === "parse_failed" && !NOT_RETRYABLE.has(u.failure_code ?? "")) || u.status === "scan_unavailable";
          const canReplace = !["infected"].includes(u.status);
          return (
            <div key={u.id} className="rounded-md border border-[var(--border)] p-3 text-sm" data-testid={`upload-${u.id}`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <span className="font-medium">{u.source_kind === "url" ? (u.final_url ?? u.filename) : u.filename}</span>
                  <span className="ml-2 text-[var(--text-muted)]">
                    {u.kind ? u.kind.toUpperCase() : "—"} · {formatSize(u.size_bytes)}
                    {u.ingest_attempt > 1 ? ` · attempt ${u.ingest_attempt}` : ""}
                  </span>
                  <span className={`badge ml-2 ${badgeClass(u.status)}`}>{STATUS_LABEL[u.status]}</span>
                  {u.superseded_by_upload_id && <span className="badge ml-2">Replaced by a newer upload</span>}
                </div>
                <div className="flex flex-wrap gap-2">
                  {u.status === "parsed" && (
                    <button className="btn-secondary" onClick={() => toggleDetails(u.id)}>
                      {detail ? "Hide details" : "Details"}
                    </button>
                  )}
                  {canRetry && (
                    <button className="btn-secondary" disabled={busyId === u.id} onClick={() => act(u.id, async () => examEndpoints.retryKtUpload(await getToken(), u.id))}>
                      Retry
                    </button>
                  )}
                  {canReplace && u.status !== "rejected" && (
                    <label className="btn-secondary cursor-pointer">
                      Replace
                      <input
                        className="sr-only"
                        type="file"
                        accept={ACCEPT}
                        aria-label={`Replace ${u.filename}`}
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          event.target.value = "";
                          if (file) void sendFiles([file], u.id);
                        }}
                      />
                    </label>
                  )}
                  {u.status !== "rejected" &&
                    (confirmDeleteId === u.id ? (
                      <>
                        <button
                          className="btn-secondary"
                          disabled={busyId === u.id}
                          onClick={() => {
                            setConfirmDeleteId(null);
                            void act(u.id, async () => {
                              await examEndpoints.deleteKtUpload(await getToken(), u.id);
                              onSourcesChanged?.();
                            });
                          }}
                        >
                          Confirm delete
                        </button>
                        <button className="btn-secondary" onClick={() => setConfirmDeleteId(null)}>
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button className="btn-secondary" onClick={() => setConfirmDeleteId(u.id)}>
                        Delete
                      </button>
                    ))}
                </div>
              </div>

              {u.failure_reason && u.status !== "parsed" && (
                <p className="mt-2 text-[var(--error)]">
                  {u.failure_reason}
                </p>
              )}

              {detail && (
                <div className="mt-2 border-t border-[var(--border)] pt-2">
                  <p>
                    {detail.versions.at(-1)?.anchor_count ?? 0} passages extracted
                    {detail.versions.at(-1)?.total_pages ? ` from ${detail.versions.at(-1)?.total_pages} pages` : ""}.
                    {detail.versions.at(-1)?.truncated ? " The document was longer than the limit; the end was not read." : ""}
                  </p>
                  {warnings.length > 0 ? (
                    <div className="mt-1">
                      <p className="font-medium">Not read — review before relying on this source:</p>
                      <ul className="list-disc pl-5">
                        {warnings.map((w) => (
                          <li key={w.code}>{w.message}</li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <p className="mt-1 text-[var(--text-muted)]">No unread content was detected.</p>
                  )}
                  {detail.sources.length === 0 && (
                    <button
                      className="btn-secondary mt-2"
                      disabled={busyId === u.id}
                      onClick={() =>
                        act(u.id, async () => {
                          const token = await getToken();
                          await examEndpoints.registerSourceFromUpload(token, u.id);
                          const fresh = await examEndpoints.getKtUpload(token, u.id);
                          setDetails((prev) => ({ ...prev, [u.id]: fresh }));
                          onSourcesChanged?.();
                        })
                      }
                    >
                      Use as reference material
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
