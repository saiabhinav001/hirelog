import Link from "next/link";

export default function Home() {
  return (
    <div className="pb-24">
      <section className="page-container grid gap-12 pb-16 pt-20 lg:grid-cols-[1.1fr_0.9fr]">
        <div className="space-y-8">
          <div className="eyebrow">
            Fortune-grade interview intelligence
          </div>
          <h1 className="font-display text-4xl leading-tight sm:text-5xl">
            Placement intelligence that scales with every hiring season.
          </h1>
          <p className="text-lg text-[var(--text-muted)]">
            The Placement Archive turns interview notes into board-ready
            insights, semantic search, and preparation dashboards so teams stay
            ahead of every placement cycle.
          </p>
          <div className="flex flex-wrap gap-3">
            <Link href="/signup" className="btn-primary">
              Request access
            </Link>
            <Link href="/search" className="btn-secondary">
              Explore search
            </Link>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            {[
              ["180+ signals", "AI-extracted topics and questions"],
              ["24x faster", "Semantic matches over raw notes"],
              ["Role-ready", "Contributor vs viewer governance"],
            ].map(([title, description]) => (
              <div key={title} className="stat-card">
                <p className="font-display text-2xl">{title}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.2em] text-[var(--text-muted)]">
                  {description}
                </p>
              </div>
            ))}
          </div>
        </div>

        <div className="card-glass relative overflow-hidden p-8 shimmer">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-[var(--text-muted)]">
                Executive snapshot
              </p>
              <p className="mt-2 text-xl font-semibold">
                Software Engineer Intern · 2025
              </p>
            </div>
            <span className="pill bg-white/90">Difficulty: Medium</span>
          </div>
          <p className="mt-6 text-sm text-[var(--text-muted)]">
            Two DSA challenges, deep DBMS normalization, and a leadership story
            on team conflict. Candidate highlighted project ownership and
            scaling tradeoffs.
          </p>
          <div className="mt-6 flex flex-wrap gap-2">
            {["DSA", "DBMS", "HR", "System Design"].map((topic) => (
              <span
                key={topic}
                className="rounded-full bg-white/80 px-3 py-1 text-xs font-semibold text-[var(--text-muted)]"
              >
                {topic}
              </span>
            ))}
          </div>
          <div className="mt-6 rounded-2xl border border-[var(--border)] bg-white/80 p-4">
            <p className="text-xs uppercase tracking-[0.2em] text-[var(--text-muted)]">
              Semantic query
            </p>
            <p className="mt-2 text-sm font-semibold text-[var(--text)]">
              “Graphs, transactions, and leadership questions”
            </p>
            <div className="mt-3 h-2 w-full rounded-full bg-[var(--surface-muted)]">
              <div className="h-2 w-4/5 rounded-full bg-[var(--primary)]" />
            </div>
          </div>
        </div>
      </section>

      <section className="page-container pb-16">
        <div className="card px-8 py-6">
          <div className="flex flex-wrap items-center justify-between gap-6">
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-[var(--text-muted)]">
                Trusted by placement teams
              </p>
              <p className="mt-2 text-lg font-semibold">
                Built for institutions that run at scale.
              </p>
            </div>
            <div className="flex flex-wrap gap-4 text-xs font-semibold uppercase tracking-[0.2em] text-[var(--text-muted)]">
              {["Tech", "Consulting", "FinTech", "Analytics", "SaaS"].map(
                (label) => (
                  <span key={label} className="pill">
                    {label}
                  </span>
                )
              )}
            </div>
          </div>
        </div>
      </section>

      <section className="page-container grid gap-6 pb-16 md:grid-cols-3">
        {[
          {
            title: "Structured extraction",
            description:
              "Auto-pulls questions, topics, and difficulty from raw notes with NLP.",
          },
          {
            title: "Semantic search",
            description:
              "FAISS-powered embeddings surface similar interviews instantly.",
          },
          {
            title: "Preparation dashboards",
            description:
              "Track topic frequency, difficulty spread, and prep insights.",
          },
        ].map((feature) => (
          <div key={feature.title} className="card p-6">
            <h3 className="font-display text-xl">{feature.title}</h3>
            <p className="mt-2 text-sm text-[var(--text-muted)]">
              {feature.description}
            </p>
          </div>
        ))}
      </section>

      <section className="page-container pb-16">
        <div className="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
          <div className="card p-8">
            <h2 className="font-display text-2xl">Operational control</h2>
            <p className="mt-2 text-sm text-[var(--text-muted)]">
              Role-based access, end-to-end auditability, and a single source of
              truth in Firestore keep your preparation data secure and accurate.
            </p>
            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              {[
                ["Governance", "Viewer vs contributor permissions enforced."],
                ["Quality", "NLP cleans and normalizes every submission."],
                ["Search-ready", "Embeddings refreshed with every upload."],
                ["Insights", "Dashboards summarize what matters most."],
              ].map(([title, description]) => (
                <div key={title} className="card-muted p-4">
                  <p className="font-semibold text-[var(--text)]">{title}</p>
                  <p className="mt-1 text-sm text-[var(--text-muted)]">
                    {description}
                  </p>
                </div>
              ))}
            </div>
          </div>

          <div className="card-glass p-8">
            <p className="text-xs uppercase tracking-[0.2em] text-[var(--text-muted)]">
              Insight highlights
            </p>
            <div className="mt-6 space-y-4">
              {[
                "DSA + DBMS is the top pairing across 2024 roles.",
                "System design rounds show highest difficulty spikes.",
                "HR signals are strongest in final-round summaries.",
              ].map((item) => (
                <div key={item} className="rounded-2xl border border-[var(--border)] bg-white/80 p-4 text-sm text-[var(--text-muted)]">
                  {item}
                </div>
              ))}
            </div>
            <Link href="/dashboard" className="btn-secondary mt-6 inline-flex">
              View dashboards
            </Link>
          </div>
        </div>
      </section>

      <section className="page-container">
        <div className="card flex flex-col gap-6 p-10 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="font-display text-2xl">
              Ready to capture your next interview cycle?
            </h2>
            <p className="mt-2 text-sm text-[var(--text-muted)]">
              Submit an experience once, and let the archive power every future
              prep session.
            </p>
          </div>
          <Link href="/submit" className="btn-primary text-center">
            Submit an experience
          </Link>
        </div>
      </section>
    </div>
  );
}
