"use client";

import Link from "next/link";
import { useAuth } from "@/context/AuthContext";

export function LoadingState({ message }: { message?: string }) {
  return (
    <div className="page-container py-12">
      <div className="skeleton skeleton-heading w-40" />
      <div className="mt-2 skeleton skeleton-text w-64" />
      <div className="mt-8 space-y-4">
        <div className="skeleton skeleton-card" />
        <div className="skeleton skeleton-card" />
        <div className="skeleton skeleton-card" />
      </div>
      {message && (
        <p className="mt-4 text-sm text-[var(--text-muted)]">{message}</p>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: { label: string; href: string };
}) {
  return (
    <div className="page-container py-12">
      <div className="card p-8 text-center max-w-md mx-auto">
        <h3 className="text-lg font-semibold">{title}</h3>
        <p className="mt-2 text-sm text-[var(--text-muted)]">{description}</p>
        {action && (
          <Link className="btn-primary mt-4 inline-flex" href={action.href}>
            {action.label}
          </Link>
        )}
      </div>
    </div>
  );
}

export function ErrorState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="page-container py-12">
      <div className="card p-6 text-center max-w-md mx-auto">
        <h3 className="text-lg font-semibold text-[var(--error)]">{title}</h3>
        <p className="mt-2 text-sm text-[var(--text-muted)]">{description}</p>
        <button 
          className="btn-secondary mt-4" 
          onClick={() => window.location.reload()}
        >
          Retry
        </button>
      </div>
    </div>
  );
}

export function AccessDenied({ onUpgrade }: { onUpgrade?: () => void }) {
  const { profile } = useAuth();

  return (
    <div className="page-container py-12">
      <div className="card p-8 text-center max-w-md mx-auto">
        <h3 className="text-lg font-semibold">Contributor access required</h3>
        <p className="mt-2 text-sm text-[var(--text-muted)]">
          Your role is <span className="badge">{profile?.role ?? "viewer"}</span>.
          Switch to contributor to submit experiences.
        </p>
        {onUpgrade && (
          <button className="btn-primary mt-4" onClick={onUpgrade}>
            Become contributor
          </button>
        )}
      </div>
    </div>
  );
}
