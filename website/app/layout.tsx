import type { Metadata } from "next";
import { headers } from "next/headers";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host =
    requestHeaders.get("x-forwarded-host") ??
    requestHeaders.get("host") ??
    "127.0.0.1:4173";
  const protocol =
    requestHeaders.get("x-forwarded-proto") ??
    (host.startsWith("127.0.0.1") || host.startsWith("localhost") ? "http" : "https");

  return {
    metadataBase: new URL(`${protocol}://${host}`),
    title: {
      default: "Anet — Private infrastructure for agent networks",
      template: "%s · Anet",
    },
    description:
      "A private, encrypted store-and-forward fabric for agents and human edge nodes.",
    openGraph: {
      title: "Anet — Private infrastructure for agent networks",
      description: "Own the identity. Seal the message. Choose the path.",
      type: "website",
      images: [{ url: "/og.png", width: 1672, height: 942, alt: "Anet private infrastructure for agent networks" }],
    },
    twitter: {
      card: "summary_large_image",
      title: "Anet — Private infrastructure for agent networks",
      description: "Own the identity. Seal the message. Choose the path.",
      images: ["/og.png"],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
