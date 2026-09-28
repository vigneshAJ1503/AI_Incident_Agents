import type { Metadata, Viewport } from "next";
import type * as React from "react";

import { AppShell } from "@/components/layout/app-shell";
import { Providers } from "@/components/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "AI Ops · Incident agents", template: "%s · AI Ops" },
  description:
    "AI-powered incident investigation: specialist agents collect evidence and an RCA agent explains it.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f8fafc" },
    { media: "(prefers-color-scheme: dark)", color: "#12151f" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
