"use client";

import { createContext, useContext, useEffect, useState, useCallback } from "react";

type Theme = "light" | "dark";

interface ThemeContextType {
  theme: Theme;
  resolvedTheme: "light" | "dark";
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>("light");
  const [resolvedTheme, setResolvedTheme] = useState<"light" | "dark">("light");
  const [mounted, setMounted] = useState(false);

  const sanitizeTheme = useCallback((raw: string | null): Theme => {
    return raw === "dark" ? "dark" : "light";
  }, []);

  const applyTheme = useCallback((newTheme: Theme) => {
    setResolvedTheme(newTheme);
    
    const root = document.documentElement;
    root.classList.remove("light", "dark");
    root.classList.add(newTheme);
    root.style.colorScheme = newTheme;
  }, []);

  const setTheme = useCallback((newTheme: Theme) => {
    setThemeState(newTheme);
    localStorage.setItem("theme", newTheme);
    applyTheme(newTheme);
  }, [applyTheme]);

  useEffect(() => {
    const initial = sanitizeTheme(localStorage.getItem("theme"));
    queueMicrotask(() => {
      setThemeState(initial);
      applyTheme(initial);
      setMounted(true);
    });
  }, [applyTheme, sanitizeTheme]);

  // Prevent flash of incorrect theme
  if (!mounted) {
    return (
      <ThemeContext.Provider value={{ theme: "light", resolvedTheme: "light", setTheme }}>
        {children}
      </ThemeContext.Provider>
    );
  }

  return (
    <ThemeContext.Provider value={{ theme, resolvedTheme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (context === undefined) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return context;
}
