import type { Metadata } from "next";
import type { ReactNode } from "react";

import "@marketing-ops/ui/tokens.css";

import "./globals.css";

export const metadata: Metadata = {
  description: "Campaign project management and social automation in one operating system.",
  title: "Marketing Ops",
};

export default function RootLayout({ children }: { readonly children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
