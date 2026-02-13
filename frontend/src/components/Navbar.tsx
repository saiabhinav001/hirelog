"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useAuth } from "@/context/AuthContext";
import { ThemeToggle } from "@/components/ThemeToggle";

const navLinks = [
  { href: "/search", label: "Search" },
  { href: "/practice", label: "Practice" },
  { href: "/submit", label: "Contribute" },
  { href: "/dashboard", label: "Analytics" },
];

// ─────────────────────────────────────────────────────────────────────────────
// Avatar Dropdown
// ─────────────────────────────────────────────────────────────────────────────

function AvatarDropdown() {
  const { profile, user, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const initial = (profile?.name || user?.email || "U").charAt(0).toUpperCase();

  // Close dropdown when clicking outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((prev) => !prev)}
        className="flex h-8 w-8 items-center justify-center rounded-full bg-[var(--primary)] text-sm font-semibold text-white transition-shadow hover:ring-2 hover:ring-[var(--primary)]/40 focus:outline-none focus:ring-2 focus:ring-[var(--primary)]/40"
        aria-label="User menu"
      >
        {initial}
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-52 rounded-lg border border-[var(--border)] bg-[var(--surface-elevated)] shadow-lg py-1 z-50 animate-in fade-in slide-in-from-top-1 duration-150">
          {/* User info header */}
          <div className="px-4 py-2.5 border-b border-[var(--border)]">
            <p className="text-sm font-medium truncate">{profile?.name || "User"}</p>
            <p className="text-xs text-[var(--text-muted)] truncate">{user?.email}</p>
          </div>

          <Link
            href="/profile"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 px-4 py-2 text-sm text-[var(--text-secondary)] hover:text-[var(--text)] hover:bg-[var(--surface-hover)] transition-colors"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" />
            </svg>
            Profile
          </Link>
          <Link
            href="/contributions"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 px-4 py-2 text-sm text-[var(--text-secondary)] hover:text-[var(--text)] hover:bg-[var(--surface-hover)] transition-colors"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
            </svg>
            My Contributions
          </Link>

          <div className="border-t border-[var(--border)] mt-1 pt-1">
            <button
              onClick={() => { setOpen(false); signOut(); }}
              className="flex w-full items-center gap-2.5 px-4 py-2 text-sm text-[var(--text-muted)] hover:text-[var(--error)] hover:bg-[var(--surface-hover)] transition-colors"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15m3 0l3-3m0 0l-3-3m3 3H9" />
              </svg>
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Mobile Menu
// ─────────────────────────────────────────────────────────────────────────────

function MobileMenu() {
  const pathname = usePathname();
  const { user, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Close on navigation
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <div className="md:hidden relative" ref={ref}>
      <button
        onClick={() => setOpen((prev) => !prev)}
        className="flex h-8 w-8 items-center justify-center rounded-md text-[var(--text-muted)] hover:text-[var(--text)] hover:bg-[var(--surface-muted)] transition-colors"
        aria-label="Menu"
      >
        {open ? (
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        ) : (
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
          </svg>
        )}
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-56 rounded-lg border border-[var(--border)] bg-[var(--surface-elevated)] shadow-lg py-1 z-50 animate-in fade-in slide-in-from-top-1 duration-150">
          {navLinks.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`block px-4 py-2.5 text-sm transition-colors ${
                pathname === item.href
                  ? "text-[var(--text)] bg-[var(--surface-muted)] font-medium"
                  : "text-[var(--text-secondary)] hover:text-[var(--text)] hover:bg-[var(--surface-hover)]"
              }`}
            >
              {item.label}
            </Link>
          ))}

          {user && (
            <>
              <div className="border-t border-[var(--border)] mt-1 pt-1">
                <Link
                  href="/profile"
                  className={`block px-4 py-2.5 text-sm transition-colors ${
                    pathname === "/profile"
                      ? "text-[var(--text)] bg-[var(--surface-muted)] font-medium"
                      : "text-[var(--text-secondary)] hover:text-[var(--text)] hover:bg-[var(--surface-hover)]"
                  }`}
                >
                  Profile
                </Link>
                <Link
                  href="/contributions"
                  className={`block px-4 py-2.5 text-sm transition-colors ${
                    pathname === "/contributions"
                      ? "text-[var(--text)] bg-[var(--surface-muted)] font-medium"
                      : "text-[var(--text-secondary)] hover:text-[var(--text)] hover:bg-[var(--surface-hover)]"
                  }`}
                >
                  My Contributions
                </Link>
              </div>
              <div className="border-t border-[var(--border)] mt-1 pt-1">
                <button
                  onClick={() => { setOpen(false); signOut(); }}
                  className="w-full text-left px-4 py-2.5 text-sm text-[var(--text-muted)] hover:text-[var(--error)] hover:bg-[var(--surface-hover)] transition-colors"
                >
                  Sign out
                </button>
              </div>
            </>
          )}

          {!user && (
            <div className="border-t border-[var(--border)] mt-1 pt-1 px-3 py-2 flex gap-2">
              <Link href="/login" className="btn-ghost text-sm flex-1 text-center">
                Sign in
              </Link>
              <Link href="/signup" className="btn-primary text-sm flex-1 text-center">
                Sign up
              </Link>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Navbar
// ─────────────────────────────────────────────────────────────────────────────

export function Navbar() {
  const pathname = usePathname();
  const { user, loading } = useAuth();

  return (
    <header className="sticky top-0 z-40 border-b border-[var(--border)] bg-[var(--bg)]/95 backdrop-blur-sm">
      <div className="page-container flex items-center justify-between h-14">
        <div className="flex items-center gap-8">
          <Link href="/" className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-md bg-[var(--primary)] text-xs font-semibold text-white">
              PA
            </span>
            <span className="font-semibold hidden sm:inline">The Placement Archive</span>
          </Link>

          <nav className="hidden items-center gap-1 text-sm md:flex">
            {navLinks.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`px-3 py-1.5 rounded-md transition-colors ${
                  pathname === item.href
                    ? "text-[var(--text)] bg-[var(--surface-muted)]"
                    : "text-[var(--text-muted)] hover:text-[var(--text)]"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </div>

        <div className="flex items-center gap-2">
          <ThemeToggle />
          {loading ? null : user ? (
            <>
              {/* Desktop: avatar dropdown */}
              <div className="hidden md:block">
                <AvatarDropdown />
              </div>
              {/* Mobile: hamburger */}
              <MobileMenu />
            </>
          ) : (
            <>
              <Link href="/login" className="btn-ghost text-sm hidden md:inline-flex">
                Sign in
              </Link>
              <Link href="/signup" className="btn-primary text-sm hidden md:inline-flex">
                Sign up
              </Link>
              {/* Mobile: hamburger (unauthenticated) */}
              <MobileMenu />
            </>
          )}
        </div>
      </div>
    </header>
  );
}
