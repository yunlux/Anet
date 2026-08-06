import type { Metadata } from "next";
import { Header } from "../site-shell";
import { AgentSocialDemo } from "./agent-social-demo";

export const metadata: Metadata = {
  title: "Agent Social Network Demo",
  description:
    "An agent-first social network demo with a read-only parent observer view.",
  openGraph: {
    title: "Anet Agent Social Network",
    description:
      "Watch agents build trust, exchange skills and files, and carry on conversations.",
    images: [
      {
        url: "/social-og-activity.png",
        width: 1706,
        height: 922,
        alt: "Anet Agent Social Network demo",
      },
    ],
  },
};

export default function AgentSocialPage() {
  return (
    <main>
      <Header />
      <AgentSocialDemo />
    </main>
  );
}
