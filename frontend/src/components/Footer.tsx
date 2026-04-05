import Link from "next/link";

export function Footer() {
  return (
    <footer className="border-t border-[var(--border)] mt-auto">
      <div className="page-container py-6 sm:py-7 flex flex-col sm:flex-row items-center justify-between gap-3 text-sm text-[var(--text-muted)]">
        <p className="text-center sm:text-left text-sm">&copy; {new Date().getFullYear()} HireLog</p>
        <nav className="flex flex-wrap justify-center gap-x-5 gap-y-2 text-sm">
          <Link href="/search" className="hover:text-[var(--text)]">Explore</Link>
          <Link href="/practice" className="hover:text-[var(--text)]">Practice</Link>
          <Link href="/submit" className="hover:text-[var(--text)]">Share</Link>
          <Link href="/dashboard" className="hover:text-[var(--text)]">Insights</Link>
        </nav>
      </div>
    </footer>
  );
}
