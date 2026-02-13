import Link from "next/link";

export default function Home() {
  return (
    <div className="page-container py-16">
      {/* Header */}
      <div className="max-w-2xl">
        <p className="text-xs font-medium uppercase tracking-widest text-[var(--primary)] mb-3">CBIT Placement Intelligence</p>
        <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight">
          The Placement Archive
        </h1>
        <p className="mt-4 text-lg text-[var(--text-secondary)] leading-relaxed">
          An institutional knowledge system that turns unstructured interview
          memories into structured, searchable, and analyzable intelligence —
          so every batch starts better prepared than the last.
        </p>
        <div className="mt-6 flex gap-3">
          <Link href="/search" className="btn-primary">
            Explore the archive
          </Link>
          <Link href="/submit" className="btn-secondary">
            Contribute an experience
          </Link>
        </div>
      </div>

      {/* Product Pipeline */}
      <div className="mt-16 p-6 rounded-lg bg-[var(--surface)] border border-[var(--border)]">
        <p className="text-xs text-[var(--text-muted)] uppercase tracking-wide mb-4">Intelligence pipeline</p>
        <div className="flex flex-wrap items-center justify-center gap-3 text-sm">
          <div className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[var(--primary)]/10 text-[var(--primary)] text-xs font-medium">1</span>
            <span className="font-medium">Raw Experience</span>
          </div>
          <svg className="h-4 w-4 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          <div className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[var(--primary)]/10 text-[var(--primary)] text-xs font-medium">2</span>
            <span className="font-medium">AI Structuring</span>
          </div>
          <svg className="h-4 w-4 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          <div className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[var(--primary)]/10 text-[var(--primary)] text-xs font-medium">3</span>
            <span className="font-medium">Semantic Discovery</span>
          </div>
          <svg className="h-4 w-4 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          <div className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[var(--success)]/20 text-[var(--success)] text-xs font-medium">✓</span>
            <span className="font-medium text-[var(--success)]">Institutional Knowledge</span>
          </div>
        </div>
      </div>

      {/* Stats */}
      <div className="mt-12 grid gap-px bg-[var(--border)] rounded-lg overflow-hidden sm:grid-cols-3">
        {[
          { value: "100+", label: "Interview experiences archived" },
          { value: "10+", label: "Topics auto-classified" },
          { value: "<1s", label: "Semantic search latency" },
        ].map((stat) => (
          <div key={stat.label} className="bg-[var(--surface)] p-5">
            <p className="text-2xl font-semibold">{stat.value}</p>
            <p className="mt-1 text-sm text-[var(--text-muted)]">{stat.label}</p>
          </div>
        ))}
      </div>

      {/* Features */}
      <div className="mt-16">
        <h2 className="text-lg font-semibold">Capabilities</h2>
        <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[
            {
              title: "Intent-based semantic search",
              description: "Describe what you're looking for in natural language. The system finds relevant experiences even when wording differs — meaning over keywords.",
            },
            {
              title: "Automated question extraction",
              description: "NLP pipeline extracts interview questions, classifies topics, and generates summaries from raw unstructured text.",
            },
            {
              title: "Placement Cell analytics",
              description: "Topic trends, difficulty distributions, company breakdowns, and repeated questions — a decision-support dashboard for institutional planning.",
            },
            {
              title: "Compounding knowledge base",
              description: "Every contribution is permanent. Data grows richer year after year as each batch of seniors contributes back.",
            },
            {
              title: "Interview progression flows",
              description: "Visualize how companies structure their interview rounds and which topics appear at each stage.",
            },
            {
              title: "Anonymous contributions",
              description: "Submit experiences anonymously to encourage honest, detailed accounts without social pressure.",
            },
          ].map((feature) => (
            <div key={feature.title} className="card p-5">
              <h3 className="font-medium">{feature.title}</h3>
              <p className="mt-2 text-sm text-[var(--text-muted)] leading-relaxed">
                {feature.description}
              </p>
            </div>
          ))}
        </div>
      </div>

      {/* How it works */}
      <div className="mt-16">
        <h2 className="text-lg font-semibold">How it works</h2>
        <div className="mt-6 grid gap-4 sm:grid-cols-4">
          {[
            { step: "1", title: "Contribute", description: "Seniors share interview experiences in plain text after their placement process." },
            { step: "2", title: "Structure", description: "AI extracts questions, classifies topics, generates embeddings and summaries." },
            { step: "3", title: "Discover", description: "Future batches search by intent and find relevant experiences across years." },
            { step: "4", title: "Analyze", description: "Placement Cell and students use analytics to identify trends and prepare strategically." },
          ].map((item) => (
            <div key={item.step} className="flex gap-4">
              <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-[var(--surface-muted)] text-sm font-medium text-[var(--text-muted)]">
                {item.step}
              </div>
              <div>
                <p className="font-medium">{item.title}</p>
                <p className="mt-1 text-sm text-[var(--text-muted)]">{item.description}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
