"use client";

import { useRouter } from "next/navigation";
import { useState, useCallback, useEffect } from "react";

import { apiFetch } from "@/lib/api";

const difficultyOptions = ["", "Easy", "Medium", "Hard"];

const EXAMPLE_QUERIES = [
  { label: "JP Morgan DBMS questions", params: { q: "DBMS normalization joins", company: "JP Morgan" } },
  { label: "Intern interviews 2024", params: { role: "Intern", year: "2024" } },
  { label: "DSA at FAANG", params: { q: "data structures algorithms", topic: "DSA" } },
  { label: "System Design rounds", params: { q: "system design scalability", topic: "System Design" } },
  { label: "Companies heavy on OS", params: { topic: "OS" } },
];

type SearchFacetsResponse = {
  generated_at?: string;
  top_topics?: string[];
  top_companies?: string[];
  trending_queries?: Record<string, number>;
};

export default function SearchPage() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [year, setYear] = useState("");
  const [topic, setTopic] = useState("");
  const [difficulty, setDifficulty] = useState("");
  const [isSearching, setIsSearching] = useState(false);
  const [showFilters, setShowFilters] = useState(false);
  const [facets, setFacets] = useState<SearchFacetsResponse | null>(null);

  useEffect(() => {
    let mounted = true;
    apiFetch<SearchFacetsResponse>("/api/search/facets", { method: "GET" })
      .then((data) => {
        if (mounted) {
          setFacets(data);
        }
      })
      .catch(() => {
        if (mounted) {
          setFacets(null);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  const handleSearch = useCallback((event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim() && !company.trim() && !role.trim() && !topic.trim()) return;
    
    setIsSearching(true);
    const params = new URLSearchParams();
    if (query.trim()) params.set("q", query.trim());
    if (company.trim()) params.set("company", company.trim());
    if (role.trim()) params.set("role", role.trim());
    if (year.trim()) params.set("year", year.trim());
    if (topic.trim()) params.set("topic", topic.trim());
    if (difficulty) params.set("difficulty", difficulty);
    router.push(`/results?${params.toString()}`);
  }, [query, company, role, year, topic, difficulty, router]);

  const handleExampleClick = (example: typeof EXAMPLE_QUERIES[0]) => {
    const params = new URLSearchParams();
    if (example.params.q) params.set("q", example.params.q);
    if (example.params.company) params.set("company", example.params.company);
    if (example.params.role) params.set("role", example.params.role);
    if (example.params.year) params.set("year", example.params.year);
    if (example.params.topic) params.set("topic", example.params.topic);
    router.push(`/results?${params.toString()}`);
  };

  const activeFilters = [company, role, year, topic, difficulty].filter(Boolean).length;

  return (
    <div className="page-container py-12">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold">Search the Archive</h1>
        <p className="mt-2 text-[var(--text-muted)]">
          Search by meaning across the archive. Describe what you are looking for and the system intelligently blends semantic and lexical evidence.
        </p>
        {facets?.generated_at && (
          <p className="mt-1 text-xs text-[var(--text-muted)] num-tabular">
            Fresh data snapshot: {new Date(facets.generated_at).toLocaleString()}
          </p>
        )}
      </div>

      <form onSubmit={handleSearch} className="mt-8 max-w-2xl">
        {/* Search input */}
        <div className="relative">
          <input
            className="input-field !h-12 !pl-11 !pr-4"
            placeholder="e.g. OS concepts asked in service companies..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search query"
          />
          <svg 
            className="absolute left-4 top-1/2 -translate-y-1/2 h-4 w-4 text-[var(--text-muted)]" 
            fill="none" 
            viewBox="0 0 24 24" 
            stroke="currentColor" 
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
          </svg>
        </div>

        <div className="mt-4 flex items-center gap-2">
          <span className="text-xs text-[var(--text-muted)] hidden sm:inline">
            Intelligent mode: semantic ranking with automatic keyword fallback
          </span>
          <button
            type="button"
            onClick={() => setShowFilters(!showFilters)}
            className={`btn-ghost text-sm ${activeFilters > 0 ? "text-[var(--primary)]" : ""}`}
            aria-expanded={showFilters}
            aria-controls="search-filters"
          >
            Filters{activeFilters > 0 ? ` (${activeFilters})` : ""}
          </button>
        </div>

        {/* Filters */}
        {showFilters && (
          <div id="search-filters" className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 p-4 rounded-lg bg-[var(--surface)] border border-[var(--border)]">
            <div>
              <label className="label">Company</label>
              <input
                className="input-field"
                value={company}
                onChange={(e) => setCompany(e.target.value)}
                placeholder="e.g. Google"
                aria-label="Company filter"
              />
            </div>
            <div>
              <label className="label">Role</label>
              <input
                className="input-field"
                value={role}
                onChange={(e) => setRole(e.target.value)}
                placeholder="e.g. SDE"
                aria-label="Role filter"
              />
            </div>
            <div>
              <label className="label">Year</label>
              <input
                className="input-field"
                type="number"
                value={year}
                onChange={(e) => setYear(e.target.value)}
                placeholder="e.g. 2024"
                aria-label="Year filter"
              />
            </div>
            <div>
              <label className="label">Topics</label>
              <input
                className="input-field"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                placeholder="DSA, DBMS..."
                aria-label="Topic filter"
              />
            </div>
            <div>
              <label className="label">Difficulty</label>
              <select
                className="input-field"
                value={difficulty}
                onChange={(e) => setDifficulty(e.target.value)}
                aria-label="Difficulty filter"
              >
                {difficultyOptions.map((opt) => (
                  <option key={opt} value={opt}>{opt || "Any"}</option>
                ))}
              </select>
            </div>
          </div>
        )}

        {/* Search button */}
        <div className="mt-6">
          <button className="btn-primary" disabled={isSearching}>
            {isSearching ? (
              <>
                <svg className="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
                Searching...
              </>
            ) : (
              "Search"
            )}
          </button>
        </div>

        {/* Tips - minimal */}
        <p className="mt-6 text-xs text-[var(--text-muted)]">
          Intelligent mode runs hybrid retrieval in parallel and surfaces source labels, AI processing state, and confidence-aware ranking.
        </p>

        {(facets?.top_topics?.length || facets?.top_companies?.length) && (
          <div className="mt-6 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-4">
            <p className="text-xs font-medium text-[var(--text-muted)] mb-2">Popular filters</p>
            <div className="flex flex-wrap gap-2">
              {(facets.top_topics || []).slice(0, 6).map((topicItem) => (
                <button
                  key={`topic-${topicItem}`}
                  type="button"
                  className="badge hover:border-[var(--primary)]"
                  onClick={() => {
                    setTopic(topicItem);
                    setShowFilters(true);
                  }}
                >
                  Topic: {topicItem}
                </button>
              ))}
              {(facets.top_companies || []).slice(0, 6).map((companyItem) => (
                <button
                  key={`company-${companyItem}`}
                  type="button"
                  className="badge hover:border-[var(--primary)]"
                  onClick={() => {
                    setCompany(companyItem);
                    setShowFilters(true);
                  }}
                >
                  Company: {companyItem}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Example queries */}
        <div className="mt-8 pt-6 border-t border-[var(--border)]">
          <p className="text-xs font-medium text-[var(--text-muted)] mb-3">Example queries from the archive</p>
          <div className="flex flex-wrap gap-2">
            {EXAMPLE_QUERIES.map((example) => (
              <button
                key={example.label}
                type="button"
                onClick={() => handleExampleClick(example)}
                className="inline-flex items-center gap-1.5 rounded-full bg-[var(--surface)] border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:border-[var(--primary)] hover:text-[var(--primary)] transition-colors"
              >
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
                </svg>
                {example.label}
              </button>
            ))}
          </div>
        </div>
      </form>
    </div>
  );
}
