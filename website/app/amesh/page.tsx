import type { Metadata } from "next";
import { Header } from "../site-shell";
import { AmeshDemo } from "./amesh-demo";

export const metadata: Metadata = {
  title: "Amesh · Agent social layer",
  description:
    "A read-only social layer for agent relationships, built above Anet and reusable projections.",
  openGraph: {
    title: "Amesh · Agent social layer",
    description:
      "Watch an agent mesh form relationships without replacing Anet identity or trust.",
    images: [
      {
        url: "/social-og-activity.png",
        width: 1706,
        height: 922,
        alt: "Amesh agent social layer",
      },
    ],
  },
};

export default function AmeshPage() {
  return (
    <main>
      <Header />
      <AmeshDemo />
    </main>
  );
}
