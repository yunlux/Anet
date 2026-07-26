import type { Metadata } from "next";
import { Header } from "../site-shell";
import { SocialCircleDemo } from "./social-circle-demo";

export const metadata: Metadata = {
  title: "Social Circle Demo",
  description:
    "An interactive Anet demo for replaying how an agent observes actors, revises subject hypotheses, and explicitly decides relationship changes.",
  openGraph: {
    title: "Anet Relations — A Small Social World",
    description:
      "Verifiable actors. Local subject hypotheses. Replayable relationship decisions.",
    images: [
      {
        url: "/social-og-activity.png",
        width: 1706,
        height: 922,
        alt: "Anet Relations social circle demo",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Anet Relations — A Small Social World",
    description:
      "Verifiable actors. Local subject hypotheses. Replayable relationship decisions.",
    images: ["/social-og-activity.png"],
  },
};

export default function SocialDemoPage() {
  return (
    <main>
      <Header />
      <SocialCircleDemo />
    </main>
  );
}
