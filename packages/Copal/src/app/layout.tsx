import type { Metadata } from "next";
import "./globals.css";
import { Toaster } from "@/components/ui/toaster";

export const metadata: Metadata = {
  title: "Copal · Local Knowledge Vault",
  description:
    "Local-first Obsidian-class vault workspace with live Markdown editing, graph views, tasks, bases, wiki blocks, and Servo-only desktop target.",
  keywords: ["Copal", "vault", "markdown", "notes", "graph", "tasks", "Servo"],
  authors: [{ name: "Copal" }],
  icons: {
    icon: "https://z-cdn.chatglm.cn/z-ai/static/logo.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="antialiased bg-background text-foreground">
        {children}
        <Toaster />
      </body>
    </html>
  );
}
