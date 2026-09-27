import "./globals.css";

import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Vigil dashboard",
  description: "Traces, runs, and regression comparison for the Vigil eval platform.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="border-b">
          <nav className="max-w-6xl mx-auto p-3 flex gap-4 text-sm" aria-label="primary">
            <Link href="/runs" className="font-semibold">
              Vigil
            </Link>
            <Link href="/runs">Runs</Link>
            <Link href="/compare">Compare</Link>
          </nav>
        </header>
        <main className="max-w-6xl mx-auto p-4">{children}</main>
      </body>
    </html>
  );
}
