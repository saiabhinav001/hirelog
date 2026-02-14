import Link from "next/link";

export function Footer() {
  return (
    <footer className="border-t border-[var(--border)] mt-auto">
      <div className="page-container py-4 sm:py-6 flex flex-col sm:flex-row items-center justify-between gap-3 text-sm text-[var(--text-muted)]">
        <p className="text-center sm:text-left text-xs sm:text-sm">&copy; {new Date().getFullYear()} HireLog</p>
        <nav className="flex gap-3 sm:gap-4 text-xs sm:text-sm">
          <Link href="/search" className="hover:text-[var(--text)]">Search</Link>
          <Link href="/practice" className="hover:text-[var(--text)]">Practice</Link>
          <Link href="/submit" className="hover:text-[var(--text)]">Contribute</Link>
          <Link href="/dashboard" className="hover:text-[var(--text)]">Analytics</Link>
        </nav>
      </div>
    </footer>
  );
}
