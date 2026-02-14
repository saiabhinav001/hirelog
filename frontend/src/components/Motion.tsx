"use client";

import { Children, type CSSProperties, type ReactNode, type Ref } from "react";

// ─────────────────────────────────────────────────────────────────────────────
// Pure CSS motion components — no framer-motion dependency.
// Uses CSS @keyframes + animation-delay for fade / slide / stagger effects,
// and respects prefers-reduced-motion via the media query.
// ─────────────────────────────────────────────────────────────────────────────

const DURATION = "220ms";

// ─────────────────────────────────────────────────────────────────────────────
// FadeIn — generic fade + subtle slide
// ─────────────────────────────────────────────────────────────────────────────

type FadeInProps = {
  delay?: number;
  direction?: "up" | "down" | "none";
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
  ref?: Ref<HTMLDivElement>;
};

export function FadeIn({
  delay = 0,
  direction = "up",
  className,
  style,
  children,
  ref,
}: FadeInProps) {
  const y = direction === "up" ? 8 : direction === "down" ? -8 : 0;

  return (
    <div
      ref={ref}
      className={className}
      style={{
        animation: `_fadeIn ${DURATION} ease-out ${delay}s both`,
        "--_fadeY": `${y}px`,
        ...style,
      } as CSSProperties}
    >
      {children}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// StaggerContainer + StaggerItem — for list animations
// ─────────────────────────────────────────────────────────────────────────────

export function StaggerContainer({
  children,
  className,
  staggerDelay = 0.05,
}: {
  children: ReactNode;
  className?: string;
  staggerDelay?: number;
}) {
  return (
    <div className={className}>
      {Children.map(children, (child, i) => {
        if (!child) return null;
        // Wrap each child with a stagger delay via CSS custom property
        return (
          <div
            style={{
              animation: `_fadeIn ${DURATION} ease-out ${i * staggerDelay}s both`,
              "--_fadeY": "6px",
            } as CSSProperties}
          >
            {child}
          </div>
        );
      })}
    </div>
  );
}

export function StaggerItem({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  // When used inside StaggerContainer the parent already applies the
  // animation wrapper. StaggerItem just passes through with its className.
  return <div className={className}>{children}</div>;
}

// ─────────────────────────────────────────────────────────────────────────────
// ScaleOnHover — subtle press/hover feedback (pure CSS)
// ─────────────────────────────────────────────────────────────────────────────

export function ScaleOnHover({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`_scaleOnHover ${className ?? ""}`}
    >
      {children}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// PageTransition — wraps page-level content
// ─────────────────────────────────────────────────────────────────────────────

export function PageTransition({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={className}
      style={{
        animation: `_fadeIn 250ms ease-out both`,
        "--_fadeY": "4px",
      } as CSSProperties}
    >
      {children}
    </div>
  );
}
