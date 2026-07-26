import type { Metadata } from "next";
import { Header } from "../site-shell";
import { SocialCircleDemo } from "./social-circle-demo";

export const metadata: Metadata = {
  title: "Social Circle Demo",
  description:
    "An interactive Anet demo showing how an agent estimates subjects, relationships, trust, and social circles.",
  openGraph: {
    title: "Anet Relations — A Small Social World",
    description:
      "Local subject hypotheses. Verifiable actors. Explainable relationships.",
    images: [
      {
        url: "/social-og-suggestions.png",
        width: 1723,
        height: 913,
        alt: "Anet Relations social circle demo",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Anet Relations — A Small Social World",
    description:
      "Local subject hypotheses. Verifiable actors. Explainable relationships.",
    images: ["/social-og-suggestions.png"],
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
