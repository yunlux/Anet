import type { Metadata } from "next";
import { Header } from "../site-shell";
import { SocialCircleDemo } from "./social-circle-demo";

export const metadata: Metadata = {
  title: "Social Circle Demo",
  description:
    "An interactive Anet demo for replaying local relationship decisions and audience-bound, content-free observer disclosures.",
  openGraph: {
    title: "Anet Relations — A Small Social World",
    description:
      "Local subject hypotheses, replayable decisions, and private observer disclosures.",
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
      "Local subject hypotheses, replayable decisions, and private observer disclosures.",
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
