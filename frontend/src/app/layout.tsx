import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "@/context/AuthContext";
import { ThemeProvider } from "@/context/ThemeContext";
import { ToastProvider } from "@/context/ToastContext";
import { Footer } from "@/components/Footer";
import { Navbar } from "@/components/Navbar";
import { ErrorBoundary } from "@/components/ErrorBoundary";

const primarySans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-plex-sans",
  display: "swap",
});

const ibmPlexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "HireLog — Campus Placement Intelligence",
  description: "Turn interview experiences into searchable, analyzable placement intelligence. Semantic search, AI extraction, and analytics for smarter campus preparation.",
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
        <link rel="preconnect" href={process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000"} />
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                const stored = localStorage.getItem('theme');
                const theme = stored === 'dark' ? 'dark' : 'light';
                const resolved = theme;
                document.documentElement.classList.add(resolved);
                document.documentElement.style.colorScheme = resolved;
              })();
            `,
          }}
        />
      </head>
      <body className={`${primarySans.variable} ${ibmPlexMono.variable} antialiased`} suppressHydrationWarning>
        <ThemeProvider>
          <AuthProvider>
            <ToastProvider>
              <div className="flex min-h-screen flex-col bg-[var(--bg)] text-[var(--text)]">
                <Navbar />
                <main className="flex-1">
                  <ErrorBoundary>{children}</ErrorBoundary>
                </main>
                <Footer />
              </div>
            </ToastProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
