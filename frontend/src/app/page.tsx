import Link from "next/link";
import { FadeIn } from "@/components/Motion";

const pipelineSteps = [
  {
    step: "01",
    title: "Capture",
    description:
      "Students submit plain-language interview memories immediately after rounds.",
  },
  {
    step: "02",
    title: "Structure",
    description:
      "NLP extracts questions, normalizes metadata, and tags recurring themes.",
  },
  {
    step: "03",
    title: "Retrieve",
    description:
      "Semantic search surfaces relevant experiences even when wording differs.",
  },
  {
    step: "04",
    title: "Compound",
    description:
      "Every new batch adds signal, improving preparation quality year over year.",
  },
];

const capabilityTiles = [
  {
    title: "Intent search over real interview data",
    description:
      "Find experiences by meaning, not exact keywords, across company, role, topic, and round.",
    badge: "Search",
  },
  {
    title: "Question extraction with source transparency",
    description:
      "User-added and AI-extracted questions remain clearly separated for trust and auditability.",
    badge: "NLP",
  },
  {
    title: "Operational analytics for Placement Cell",
    description:
      "Track topic trends, difficulty shifts, and company patterns to prioritize preparation strategy.",
    badge: "Analytics",
  },
  {
    title: "Archive integrity by design",
    description:
      "Original narrative is preserved while metadata and remembered questions remain safely editable.",
    badge: "Trust",
  },
];

const archiveSignals = [
  { value: "100+", label: "Experiences archived" },
  { value: "10+", label: "Topics auto-classified" },
  { value: "<1s", label: "Typical search response" },
];

const qualityChecks = [
  {
    title: "Anonymous-first submissions",
    description: "Encourages honest, detailed writeups without social pressure.",
  },
  {
    title: "Permanent institutional memory",
    description: "Knowledge does not reset when each graduating batch leaves.",
  },
  {
    title: "Clear edit accountability",
    description: "Visibility changes and metadata edits remain traceable over time.",
  },
];

export default function Home() {
  return (
    <div className="page-container py-8 sm:py-12 lg:py-16">
      <FadeIn>
        <section className="grid gap-5 lg:grid-cols-[minmax(0,1.12fr)_minmax(0,0.88fr)] lg:items-start">
          <div className="card relative overflow-hidden p-6 sm:p-8 lg:p-10">
            <div className="pointer-events-none absolute -right-20 -top-20 h-56 w-56 rounded-full bg-[var(--primary-soft)] blur-3xl" />
            <div className="pointer-events-none absolute -bottom-20 left-1/3 h-48 w-48 rounded-full bg-[var(--success-soft)] blur-3xl" />

            <p className="relative text-xs font-semibold uppercase tracking-[0.16em] text-[var(--primary)] sm:text-sm">
              Campus Placement Intelligence
            </p>
            <h1 className="relative mt-3 max-w-3xl text-balance text-3xl font-semibold tracking-[-0.018em] sm:text-4xl lg:text-[2.9rem] lg:leading-[1.07]">
              Institutional interview memory that compounds every year.
            </h1>
            <p className="relative mt-4 max-w-2xl text-base leading-relaxed text-[var(--text-secondary)] sm:text-lg">
              HireLog converts scattered interview recollections into a trusted, searchable archive so each new batch starts from evidence, not guesswork.
            </p>

            <div className="relative mt-8 grid gap-3 sm:flex sm:flex-wrap sm:items-center">
              <Link href="/search" className="btn-primary w-full sm:w-auto">
                Explore archive
              </Link>
              <Link href="/submit" className="btn-secondary w-full sm:w-auto">
                Add experience
              </Link>
            </div>

            <dl className="relative mt-8 grid gap-3 sm:grid-cols-3">
              {archiveSignals.map((signal) => (
                <div key={signal.label} className="rounded-lg border border-[var(--border)]/80 bg-[var(--surface)]/90 px-3.5 py-3">
                  <dt className="text-xs uppercase tracking-[0.08em] text-[var(--text-muted)]">{signal.label}</dt>
                  <dd className="mt-1 text-xl font-semibold leading-none sm:text-2xl">{signal.value}</dd>
                </div>
              ))}
            </dl>
          </div>

          <div className="grid gap-4">
            <div className="card p-5 sm:p-6">
              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
                Why students trust it
              </p>
              <ul className="mt-4 space-y-3">
                {qualityChecks.map((item) => (
                  <li key={item.title} className="rounded-md border border-[var(--border)] bg-[var(--surface-muted)] px-3 py-2.5">
                    <p className="text-sm font-semibold text-[var(--text)] sm:text-base">{item.title}</p>
                    <p className="mt-1 text-sm leading-relaxed text-[var(--text-muted)]">{item.description}</p>
                  </li>
                ))}
              </ul>
            </div>

            <div className="card p-5 sm:p-6">
              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
                Knowledge quality loop
              </p>
              <div className="mt-4 space-y-2.5 text-sm sm:text-base">
                <div className="flex items-center justify-between rounded-md border border-[var(--border)] bg-[var(--surface-muted)] px-3 py-2">
                  <span className="font-medium text-[var(--text-secondary)]">Extraction quality</span>
                  <span className="badge badge-primary">Monitored</span>
                </div>
                <div className="flex items-center justify-between rounded-md border border-[var(--border)] bg-[var(--surface-muted)] px-3 py-2">
                  <span className="font-medium text-[var(--text-secondary)]">Search relevance</span>
                  <span className="badge badge-success">Improves</span>
                </div>
                <div className="flex items-center justify-between rounded-md border border-[var(--border)] bg-[var(--surface-muted)] px-3 py-2">
                  <span className="font-medium text-[var(--text-secondary)]">Visibility controls</span>
                  <span className="badge badge-warning">User-owned</span>
                </div>
              </div>
            </div>
          </div>
        </section>
      </FadeIn>

      <FadeIn delay={0.08}>
        <section className="mt-12 sm:mt-14">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <h2 className="text-balance text-2xl font-semibold tracking-tight sm:text-3xl">
              From single memory to shared preparation advantage
            </h2>
            <p className="max-w-xl text-sm text-[var(--text-muted)] sm:text-base">
              A repeatable pipeline turns unstructured recollections into durable signals for students and Placement Cell.
            </p>
          </div>
          <ol className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {pipelineSteps.map((item) => (
                <li
                  key={item.step}
                  className="card min-h-[168px] p-4 sm:p-5"
                >
                  <div className="flex items-center gap-2">
                    <span className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-[var(--primary-soft)] text-sm font-semibold text-[var(--primary)]">
                      {item.step}
                    </span>
                    <p className="text-base font-semibold text-[var(--text)] sm:text-lg">{item.title}</p>
                  </div>
                  <p className="mt-3 text-sm leading-relaxed text-[var(--text-muted)] sm:text-base">
                    {item.description}
                  </p>
                  <div className="mt-4 h-1 w-14 rounded-full bg-[var(--primary-soft)]" />
                </li>
              ))}
            </ol>
          </section>
      </FadeIn>

      <FadeIn delay={0.14}>
        <section className="mt-14 sm:mt-16">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <h2 className="text-balance text-2xl font-semibold tracking-tight sm:text-3xl">
              Built for trust, speed, and reuse
            </h2>
            <Link href="/dashboard" className="btn-ghost w-fit px-0 text-sm sm:text-base">
              View institutional dashboard
              <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <path d="M7 4L13 10L7 16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </Link>
          </div>

          <div className="mt-6 grid gap-4 sm:grid-cols-2">
            {capabilityTiles.map((tile, index) => (
              <article key={tile.title} className="card p-5 sm:p-6">
                <div className="flex items-center justify-between gap-3">
                  <span className="badge">{tile.badge}</span>
                  <span className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface-muted)] text-xs font-semibold text-[var(--text-muted)]">
                    {index + 1}
                  </span>
                </div>
                <h3 className="mt-3 text-xl font-semibold leading-tight text-balance sm:text-2xl">{tile.title}</h3>
                <p className="mt-3 text-sm leading-relaxed text-[var(--text-muted)] sm:text-base">
                  {tile.description}
                </p>
              </article>
            ))}
          </div>
        </section>
      </FadeIn>

      <FadeIn delay={0.2}>
        <section className="mt-14 sm:mt-16">
          <div className="card overflow-hidden p-6 sm:p-8">
            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
                  Start Now
                </p>
                <h2 className="mt-2 text-balance text-2xl font-semibold tracking-tight sm:text-3xl">
                  Prepare from real evidence, not recycled guess lists.
                </h2>
                <p className="mt-3 max-w-2xl text-sm leading-relaxed text-[var(--text-muted)] sm:text-base">
                  Browse by role, company, and topic to build focused preparation plans. Then contribute your own experience to strengthen the next batch.
                </p>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
                <Link href="/search" className="btn-primary w-full min-w-[11.5rem]">
                  Search experiences
                </Link>
                <Link href="/submit" className="btn-secondary w-full min-w-[11.5rem]">
                  Contribute now
                </Link>
              </div>
            </div>
          </div>
        </section>
      </FadeIn>
    </div>
  );
}
