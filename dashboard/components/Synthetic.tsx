// Labels for the prepared SYNTHETIC demo data (demo/synthetic_fixture.py). A run is labelled by
// its suite name only: the seeder reserves the `vigil-demo-synthetic-` prefix, so a real run is
// never marked synthetic by this.

export const SYNTHETIC_PREFIX = "vigil-demo-synthetic-";

export const isSyntheticSuite = (suite: string | null | undefined) =>
  typeof suite === "string" && suite.startsWith(SYNTHETIC_PREFIX);

export function SyntheticBadge({ suite }: { suite: string | null | undefined }) {
  if (!isSyntheticSuite(suite)) return null;
  return (
    <span
      className="inline-block ml-2 px-1.5 py-0.5 rounded border border-dashed text-xs font-semibold align-middle"
      title="Prepared, invented demo data: no agent ran and no model was called."
    >
      SYNTHETIC
    </span>
  );
}

// Shown on every page of a dashboard started by the demo launcher (`make demo`), which sets
// NEXT_PUBLIC_VIGIL_DEMO=1. Other dashboards never show it.
export function DemoNotice() {
  if (process.env.NEXT_PUBLIC_VIGIL_DEMO !== "1") return null;
  return (
    <div role="note" aria-label="demo notice" className="border-b border-dashed text-sm">
      <p className="max-w-6xl mx-auto px-3 py-2">
        Demo setup adds prepared synthetic examples (suites named{" "}
        <code className="font-mono text-xs">{SYNTHETIC_PREFIX}*</code>, marked SYNTHETIC); no
        model calls were made for them and their scores are invented. Other runs listed are
        whatever this database already holds. Local read-only API, no authentication: not a public
        service.
      </p>
    </div>
  );
}
