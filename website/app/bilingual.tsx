import type { ReactNode } from "react";

/** Render one language at a time using the site's persistent language toggle. */
export function T({ en, zh }: { en: ReactNode; zh: ReactNode }) {
  return (
    <>
      <span className="lang-en">{en}</span>
      <span className="lang-zh">{zh}</span>
    </>
  );
}
