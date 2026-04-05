import Link from "next/link";
import { FadeIn } from "@/components/Motion";

const pipelineSteps = [
  { step: "1", label: "Raw Experience", done: false },
  { step: "2", label: "AI Structuring", done: false },
  { step: "3", label: "Semantic Discovery", done: false },
  { step: "✓", label: "Institutional Knowledge", done: true },
];

const capabilities = [
  {
    title: "Intent-based semantic search",
    description:
      "Describe what you are looking for in natural language. The system finds relevant experiences even when wording differs - meaning over keywords.",
  },
  {
    title: "Automated question extraction",
    description:
      "NLP pipeline extracts interview questions, classifies topics, and generates summaries from raw unstructured text.",
  },
  {
    title: "Placement Cell analytics",
    description:
      "Topic trends, difficulty distributions, company breakdowns, and repeated questions for institutional planning.",
  },
  {
    title: "Compounding knowledge base",
    description:
      "Every contribution is permanent. Data grows richer year after year as each batch contributes back.",
  },
  {
    title: "Interview progression flows",
    description:
      "Visualize how companies structure interview rounds and which topics appear at each stage.",
  },
  {
    title: "Anonymous contributions",
    description:
      "Submit experiences anonymously to encourage honest, detailed accounts without social pressure.",
  },
];

const workflow = [
  {
    step: "1",
    title: "Contribute",
    description: "Seniors share interview experiences in plain text after placement.",
  },
  {
    step: "2",
    title: "Structure",
    description: "AI extracts questions, classifies topics, and generates summaries.",
  },
  {
    step: "3",
    title: "Discover",
    description: "Future batches search by intent and find relevant experiences.",
  },
  {
    step: "4",
    title: "Analyze",
    description: "Placement Cell and students use analytics for strategic preparation.",
  },
];

export default function Home() {
  return (
    <div className="page-container py-8 sm:py-12 lg:py-16">
      <FadeIn>
        <section className="max-w-3xl">
          <p className="text-xs sm:text-sm font-semibold uppercase tracking-[0.14em] text-[var(--primary)]">
            Campus Placement Intelligence
          </p>
          <h1 className="mt-2 text-4xl sm:text-5xl lg:text-6xl font-semibold tracking-tight text-balance">
            HireLog
          </h1>
          <p className="mt-4 max-w-2xl text-base sm:text-lg text-[var(--text-secondary)] leading-relaxed">
            Turn unstructured interview memories into structured, searchable, and analyzable intelligence so every batch starts better prepared than the last.
          </p>
          <div className="mt-8 grid gap-3 sm:flex sm:items-center">
            <Link href="/search" className="btn-primary w-full sm:w-auto">
              Start exploring
            </Link>
            <Link href="/submit" className="btn-secondary w-full sm:w-auto">
              Share your experience
            </Link>
          </div>
        </section>
      </FadeIn>

      <FadeIn delay={0.08}>
        <section className="mt-12 sm:mt-14">
          <div className="card p-4 sm:p-6">
            <p className="text-xs sm:text-sm text-[var(--text-muted)] uppercase tracking-[0.12em]">
              Intelligence Pipeline
            </p>
            <ol className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {pipelineSteps.map((item) => (
                <li
                  key={item.label}
                  className={`rounded-lg border px-3 py-3 sm:px-4 sm:py-3.5 min-w-0 min-h-[74px] ${
                    item.done
                      ? "bg-[var(--success-soft)] border-[var(--success-border)]"
                      : "bg-[var(--surface)] border-[var(--border)]"
                  }`}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <span
                      className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${
                        item.done
                          ? "bg-[var(--success)]/18 text-[var(--success)]"
                          : "bg-[var(--primary-soft)] text-[var(--primary)]"
                      }`}
                    >
                      {item.step}
                    </span>
                    <span className={`text-base font-medium leading-snug ${item.done ? "text-[var(--success)]" : "text-[var(--text-secondary)]"}`}>
                      {item.label}
                    </span>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </section>
      </FadeIn>

      <FadeIn delay={0.14}>
        <section className="mt-10 grid gap-3 sm:grid-cols-3">
          {[
            { value: "100+", label: "Interview experiences archived" },
            { value: "10+", label: "Topics auto-classified" },
            { value: "<1s", label: "Semantic search latency" },
          ].map((stat) => (
            <div key={stat.label} className="card p-5 sm:p-6">
              <p className="text-3xl sm:text-4xl font-semibold leading-none">{stat.value}</p>
              <p className="mt-2 text-sm sm:text-base text-[var(--text-muted)]">{stat.label}</p>
            </div>
          ))}
        </section>
      </FadeIn>

      <FadeIn delay={0.18}>
        <section className="mt-14 sm:mt-16">
          <h2 className="text-3xl sm:text-4xl font-semibold text-balance">Capabilities</h2>
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {capabilities.map((feature) => (
              <div key={feature.title} className="card p-5 sm:p-6">
                <h3 className="text-xl sm:text-2xl font-semibold leading-tight text-balance">{feature.title}</h3>
                <p className="mt-3 text-sm sm:text-base text-[var(--text-muted)] leading-relaxed">
                  {feature.description}
                </p>
              </div>
            ))}
          </div>
        </section>
      </FadeIn>

      <FadeIn delay={0.22}>
        <section className="mt-14 sm:mt-16">
          <h2 className="text-3xl sm:text-4xl font-semibold text-balance">How it works</h2>
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {workflow.map((item) => (
              <div key={item.step} className="card p-4 sm:p-5">
                <div className="flex h-9 w-9 items-center justify-center rounded-md bg-[var(--surface-muted)] text-sm font-semibold text-[var(--text-muted)]">
                  {item.step}
                </div>
                <p className="mt-3 text-lg sm:text-xl font-semibold">{item.title}</p>
                <p className="mt-2 text-sm sm:text-base text-[var(--text-muted)] leading-relaxed">
                  {item.description}
                </p>
              </div>
            ))}
          </div>
        </section>
      </FadeIn>
    </div>
  );
}
