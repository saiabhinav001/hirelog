"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useAuth } from "@/context/AuthContext";
import { ThemeToggle } from "@/components/ThemeToggle";

const baseNavLinks = [
  { href: "/search", label: "Explore" },
  { href: "/practice", label: "Practice" },
  { href: "/submit", label: "Share" },
  { href: "/dashboard", label: "Insights" },
];

const isActivePath = (pathname: string, href: string) => {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
};

// ─────────────────────────────────────────────────────────────────────────────
// Avatar Dropdown
// ─────────────────────────────────────────────────────────────────────────────

function AvatarDropdown() {
  const { profile, user, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const name = profile?.name || "User";
  const initial = (name || user?.email || "U").charAt(0).toUpperCase();

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
        className={`group flex h-10 w-10 aspect-square items-center justify-center rounded-full border text-sm font-semibold tracking-[-0.01em] transition-all ${
          open
            ? "border-[var(--primary)] bg-[linear-gradient(145deg,var(--primary),var(--primary-hover))] text-[var(--on-primary)] shadow-[0_6px_16px_rgba(31,86,214,0.28)]"
            : "border-[var(--border)] bg-[var(--primary-soft)] text-[var(--primary)] hover:border-[var(--border-hover)] hover:shadow-[0_2px_8px_rgba(15,23,42,0.12)]"
        }`}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="User menu"
      >
        {initial}
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-2 w-60 rounded-2xl border border-[var(--border)] bg-[var(--surface-elevated)] p-1.5 shadow-[0_16px_40px_rgba(15,23,42,0.18)] animate-in fade-in slide-in-from-top-1 duration-150">
          <div className="rounded-xl border border-[var(--border)]/70 bg-[var(--surface-muted)] px-3 py-2.5">
            <p className="truncate text-sm font-semibold text-[var(--text)]">{name}</p>
            <p className="mt-0.5 truncate text-xs text-[var(--text-muted)]">{user?.email}</p>
          </div>

          <Link
            href="/profile"
            onClick={() => setOpen(false)}
            className="mt-1.5 flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[0.88rem] font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" />
            </svg>
            Profile
          </Link>
          <Link
            href="/contributions"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[0.88rem] font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
            </svg>
            My Contributions
          </Link>

          <div className="mt-1 border-t border-[var(--border)] pt-1">
            <button
              onClick={() => {
                setOpen(false);
                signOut();
              }}
              className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2.5 text-[0.88rem] font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--error)]"
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
  const { user, profile, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const navLinks = profile?.role === "placement_cell"
    ? [...baseNavLinks, { href: "/placement-cell", label: "Cell Ops" }]
    : baseNavLinks;

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const prevPathname = useRef(pathname);
  useEffect(() => {
    if (prevPathname.current !== pathname) {
      prevPathname.current = pathname;
      // eslint-disable-next-line react-hooks/set-state-in-effect -- close transient menu state when route changes
      setOpen(false);
    }
  }, [pathname]);

  return (
    <div className="relative md:hidden" ref={ref}>
      <button
        onClick={() => setOpen((prev) => !prev)}
        className={`flex h-10 w-10 items-center justify-center rounded-xl border transition-colors ${
          open
            ? "border-[var(--primary)] bg-[var(--primary-soft)] text-[var(--primary)]"
            : "border-[var(--border)] bg-[var(--surface)] text-[var(--text-muted)] hover:border-[var(--border-hover)] hover:text-[var(--text)]"
        }`}
        aria-expanded={open}
        aria-haspopup="menu"
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
        <div className="absolute right-0 z-50 mt-2 w-[min(21rem,calc(100vw-1rem))] overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--surface-elevated)] shadow-[0_18px_42px_rgba(15,23,42,0.2)] animate-in fade-in slide-in-from-top-1 duration-150">
          <nav className="grid gap-1 p-2">
            {navLinks.map((item) => {
              const active = isActivePath(pathname, item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`rounded-xl px-3.5 py-2.5 text-[0.92rem] font-medium transition-colors ${
                    active
                      ? "bg-[var(--primary-soft)] text-[var(--primary)]"
                      : "text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          {user && (
            <>
              <div className="border-t border-[var(--border)] p-2">
                <Link
                  href="/profile"
                  className={`block rounded-xl px-3.5 py-2.5 text-[0.9rem] font-medium transition-colors ${
                    isActivePath(pathname, "/profile")
                      ? "bg-[var(--primary-soft)] text-[var(--primary)]"
                      : "text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
                  }`}
                >
                  Profile
                </Link>
                <Link
                  href="/contributions"
                  className={`mt-1 block rounded-xl px-3.5 py-2.5 text-[0.9rem] font-medium transition-colors ${
                    isActivePath(pathname, "/contributions")
                      ? "bg-[var(--primary-soft)] text-[var(--primary)]"
                      : "text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
                  }`}
                >
                  My Contributions
                </Link>
              </div>
              <div className="border-t border-[var(--border)] p-2">
                <button
                  onClick={() => {
                    setOpen(false);
                    signOut();
                  }}
                  className="w-full rounded-xl px-3.5 py-2.5 text-left text-[0.9rem] font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--error)]"
                >
                  Sign out
                </button>
              </div>
            </>
          )}

          {!user && (
            <div className="grid gap-2 border-t border-[var(--border)] p-2 sm:grid-cols-2">
              <Link
                href="/login"
                className={`btn-nav w-full justify-center ${
                  pathname === "/login" ? "btn-primary" : "btn-secondary"
                }`}
              >
                Sign in
              </Link>
              <Link
                href="/signup"
                className={`btn-nav w-full justify-center ${
                  pathname === "/signup" ? "btn-primary" : "btn-secondary"
                }`}
              >
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
  const { user, profile, loading } = useAuth();
  const [scrolled, setScrolled] = useState(false);
  const isAuthRoute = pathname === "/login" || pathname === "/signup";

  const navLinks = profile?.role === "placement_cell"
    ? [...baseNavLinks, { href: "/placement-cell", label: "Cell Ops" }]
    : baseNavLinks;
  const loginCtaClass = pathname === "/login" ? "btn-primary" : "btn-secondary";
  const signupCtaClass = pathname === "/signup" ? "btn-primary" : "btn-secondary";

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`sticky top-0 z-40 border-b backdrop-blur-xl transition-[border-color,box-shadow,background-color] duration-200 ${
        scrolled
          ? "border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_92%,transparent)] shadow-[0_6px_24px_rgba(15,23,42,0.09)]"
          : "border-transparent bg-[color-mix(in_srgb,var(--bg)_90%,transparent)]"
      }`}
    >
      <div className="page-container flex h-[4.25rem] items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3 lg:gap-5">
          <Link href="/" className="inline-flex items-center gap-2 rounded-xl px-1 py-1">
            <svg className="h-8 w-8" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
              <rect width="32" height="32" rx="8" fill="var(--primary)" />
              <path d="M9 8h4v16H9V8zm10 0h4v16h-4V8zm-10 6h14v4H9v-4z" fill="white" />
            </svg>
            <span className="text-base font-semibold tracking-[-0.01em]">HireLog</span>
          </Link>

          {!isAuthRoute && (
            <nav className="hidden items-center gap-1 rounded-xl border border-[var(--border)] bg-[var(--surface)] px-1.5 py-1 md:flex">
              {navLinks.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  prefetch={false}
                  className={`inline-flex h-9 items-center rounded-lg px-3 text-[0.9rem] font-medium transition-colors ${
                    isActivePath(pathname, item.href)
                      ? "bg-[var(--primary-soft)] text-[var(--primary)]"
                      : "text-[var(--text-muted)] hover:bg-[var(--surface-muted)] hover:text-[var(--text)]"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
            </nav>
          )}
        </div>

        <div className="flex items-center gap-2.5">
          <ThemeToggle />
          {loading ? null : user ? (
            <>
              <div className="hidden md:block">
                <AvatarDropdown />
              </div>
              <MobileMenu />
            </>
          ) : (
            <>
              <div className="hidden items-center gap-2 md:flex">
                <Link href="/login" className={`${loginCtaClass} btn-nav`}>
                  Sign in
                </Link>
                <Link href="/signup" className={`${signupCtaClass} btn-nav`}>
                  Sign up
                </Link>
              </div>
              <MobileMenu />
            </>
          )}
        </div>
      </div>
    </header>
  );
}
