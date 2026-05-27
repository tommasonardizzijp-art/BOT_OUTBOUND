import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Toaster } from "@/components/ui/sonner";
import LayoutShell from "@/components/LayoutShell";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "BOT OUTBOUND",
  description: "Instagram DM Automation Dashboard",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="it" className="h-full">
      <body className={`${inter.className} h-full bg-gray-950 text-gray-100`}>
        <LayoutShell>{children}</LayoutShell>
        <Toaster theme="dark" />
      </body>
    </html>
  );
}
