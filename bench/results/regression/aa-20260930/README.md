# A/A test — HotpotQA base100, agent version 0f1be87f (2026-09-30, Oracle ARM)

Outputs of `python -m engine regress` (paired bootstrap over questions, paired t CI, α=0.05).

- `A_vs_B.txt` — the planned A/A: legs A `ddbb1332` and B `8fd25cc4`, run back to back.
  **Partial:** leg B hit the Anthropic account usage limit at 00:36:18 UTC; 101 of its 300 units
  errored (HTTP 400, zero tokens) and are excluded (regress uses OK units only). The comparison
  pairs the 67 questions B completed (66 with all 3 trials, 1 with fewer) — the first 67 in suite
  order, not a random subset.
- `ideapad-baseline_vs_A.txt` — the same agent version against the 2026-09-26 measurement
  baseline `785e9e33` (IdeaPad): all 100 questions × 3 trials on both sides. Not a pure A/A
  (different machine and day), but a complete same-version pair.

## Reading the verdicts

- **No gated regression in either comparison.** In A vs B, `passed` shows **watch**: its paired t
  interval just excludes zero (Δ −0.015, p = 0.049) but the drop is below the 5% threshold. That is
  the expected false-alarm behaviour of the interval test alone: the adopted paired t CI has a
  nominal 5% false-positive rate (6.7% measured in the synthetic study,
  `../785e9e33-0992-4d02-9a3c-ea2b4f0b108f.md`), while the gated rule (interval **and** threshold)
  measured 0.0%.
- **`cost_usd` → REGRESSION in A vs B is a false alarm** on an informational (ungated) metric: both
  legs cost $0.0065 per completed unit. Informational thresholds, starting with cost, should be
  calibrated from A/A variance once the full pair exists.

Re-run leg B in full after the usage limit resets (2026-10-01 00:00 UTC) for the clean A/A.
