# A/A test (complete) — HotpotQA base100, agent version 0f1be87f (Oracle ARM)

`A_vs_B.txt` is `python -m engine regress --baseline ddbb1332 --candidate 29fea4d6`: the same
agent version run twice on the same machine, 100 questions × 3 trials on both sides, all
300/300 units OK on both.

- Leg A `ddbb1332-0edc-4430-9958-f7774e04d018` — 2026-09-30 00:2x UTC, $1.94.
- Leg B `29fea4d6-e64a-4a84-ab74-42cd35b55415` — 2026-10-01 00:10 UTC, $1.98 (full re-run of the
  leg that hit the usage limit on 09-30; see `../aa-20260930/`).

**Verdict: no gated regression, and every metric `ok`** — gated `passed` (Δ −0.010, p 0.22) and
`TokenF1` (Δ −0.002, p 0.41), and all informational metrics including `cost_usd`
(Δ +$0.0001, p 0.33) and `latency_ms` (Δ −45 ms, p 0.67). This is the real-data false-positive
check of the detector at 100q × 3: one A/A pair, zero false alarms (the synthetic study's gated
FPR was 0.0%, interval-only 6.7%; `../785e9e33-0992-4d02-9a3c-ea2b4f0b108f.md`).

Next: calibrate informational thresholds (cost first) from A/A variance; one pair gives one
draw, so the per-question trial-to-trial variance from these runs is the input, not this verdict.
