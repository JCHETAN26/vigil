"""Shared regression gate: policy, version resolution, and the comparison the CLI and API run.

Both ``engine regress`` and the dashboard API's ``/compare`` family go through this module, so
a pair of runs gets the same verdict wherever it is asked. The flow is:

  1. ``resolve_pair`` — look up both runs and pick exactly ONE agent version per side. A run
     may hold several versions (baseline + candidate in one run); results are never averaged
     across versions. With no explicit version, a run with a single version uses it; a run
     with several is refused as ambiguous.
  2. ``load_inputs`` — fetch the selected versions' raw per-(case, trial) rows plus the suite's
     case list into a JSON-serializable ``ComparisonInputs`` (what ``--save`` exports).
  3. ``compare`` — pure: build per-metric series and run ``paired_bootstrap_diff`` per metric
     under a ``Policy``, plus a coverage audit.

Denominators (``FAILED_TRIAL_POLICY``): an error/timeout trial is a failure, not a missing
value — it scores 0 on ``passed`` and on every higher-is-better quality metric the comparison
reports. Cost and latency are measured on OK trials only (a failed trial records cost 0, which
would bias the mean down).

Side availability (``side_availability``): a metric is *measured* on a side when at least one
of that side's OK trials reports it. Failure zeros never make a metric measured: a scorer only
one side's OK trials report is skipped as one-sided even if the other side's failed trials were
zero-filled for it. The exception is a side with no OK trials at all — nothing could be
measured there, so its failure zeros stand in (a crashed candidate is still a regression) but
the comparison is incomplete. A metric measured on a side yet missing from some of that side's
OK trials is partial: compared on the trials that have it, and incomplete if gated.

Verdict: ``regression`` when any gated metric regressed (adverse evidence stands even on
incomplete data); else ``incomplete`` when the comparison is not a full paired measurement
(missing cases/trials, a run that did not succeed, a side with no OK trials, a gated metric
that could not be compared or is missing on some OK trials); else ``no_regression``.
Incomplete is never reported as a pass.

Stores are duck-typed (see ``engine.db.triage.PgTriageStore``; tests use in-memory fakes).
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from engine.regression import (
    CI_METHODS,
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    METRIC_DIRECTION,
    paired_bootstrap_diff,
)

FAILED_TRIAL_POLICY = "failed_trials_score_zero_on_quality_metrics"
# Report order; metrics not listed follow alphabetically.
METRIC_ORDER = (
    "passed", "TokenF1", "ExactMatch", "RetrievalRecall@5", "RetrievalRecall@10",
    "NDCG", "AllGoldRetrieved", "cost_usd", "latency_ms",
)
DEFAULT_GATED = ("passed", "TokenF1")
DEFAULT_THRESHOLDS = {"passed": 0.05, "TokenF1": 0.03}
_OK_ONLY_METRICS = {"cost_usd", "latency_ms"}
_MAX_ERROR_CHARS = 500
_VERSION_PREFIX_MIN = 8
VERDICTS = ("regression", "no_regression", "incomplete")


class ComparisonError(ValueError):
    """A comparison that cannot be resolved. ``kind`` maps to an HTTP status in the API:
    ``not_found`` → 404, everything else → 400."""

    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


# --- policy --------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    alpha: float = 0.05
    method: str = "t"
    n_boot: int = 10000
    seed: int = 1234
    two_level: bool = False
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    gated: tuple[str, ...] = DEFAULT_GATED

    def __post_init__(self):
        if not (isinstance(self.alpha, (int, float)) and 0 < self.alpha < 0.5):
            raise ComparisonError(f"alpha must be in (0, 0.5), got {self.alpha!r}")
        if self.method not in CI_METHODS:
            raise ComparisonError(f"method must be one of {CI_METHODS}, got {self.method!r}")
        if not (isinstance(self.n_boot, int) and 100 <= self.n_boot <= 200_000):
            raise ComparisonError(f"n_boot must be an int in [100, 200000], got {self.n_boot!r}")
        if not isinstance(self.seed, int):
            raise ComparisonError(f"seed must be an int, got {self.seed!r}")
        for m, t in self.thresholds.items():
            if not (isinstance(t, (int, float)) and math.isfinite(t) and t >= 0):
                raise ComparisonError(f"threshold for {m} must be a finite number >= 0, got {t!r}")
        object.__setattr__(self, "thresholds", {k: float(v) for k, v in self.thresholds.items()})
        object.__setattr__(self, "gated", tuple(self.gated))

    def threshold(self, metric: str) -> float:
        return self.thresholds.get(metric, 0.0)

    def with_overrides(self, **kw) -> Policy:
        """Copy with the given fields replaced; ``threshold_pass``/``threshold_f1`` are
        shorthands for the two gated thresholds (the CLI flags and API query params)."""
        th = dict(self.thresholds)
        if kw.get("threshold_pass") is not None:
            th["passed"] = kw["threshold_pass"]
        if kw.get("threshold_f1") is not None:
            th["TokenF1"] = kw["threshold_f1"]
        fields_ = {k: v for k, v in kw.items() if k in _POLICY_FIELDS and v is not None}
        return Policy(**{**self.to_dict_raw(), **fields_, "thresholds": th})

    def to_dict_raw(self) -> dict:
        return {
            "alpha": self.alpha, "method": self.method, "n_boot": self.n_boot, "seed": self.seed,
            "two_level": self.two_level, "thresholds": dict(self.thresholds),
            "gated": tuple(self.gated),
        }

    def to_dict(self) -> dict:
        d = self.to_dict_raw()
        d["gated"] = list(d["gated"])
        d["failed_trials"] = FAILED_TRIAL_POLICY
        d["default_threshold"] = 0.0  # metrics without an explicit threshold
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Policy:
        if d.get("failed_trials", FAILED_TRIAL_POLICY) != FAILED_TRIAL_POLICY:
            raise ComparisonError(f"unsupported failed-trial policy {d.get('failed_trials')!r}")
        return cls(
            alpha=d["alpha"], method=d["method"], n_boot=d["n_boot"], seed=d["seed"],
            two_level=bool(d["two_level"]), thresholds=dict(d["thresholds"]),
            gated=tuple(d["gated"]),
        )


_POLICY_FIELDS = {"alpha", "method", "n_boot", "seed", "two_level"}
DEFAULT_POLICY = Policy()


# --- store protocol + version resolution ---------------------------------------------


class TriageStore(Protocol):
    async def get_run(self, run_id: str) -> dict | None: ...
    async def get_run_versions(self, run_id: str) -> list[dict]: ...
    async def get_suite_case_ids(self, suite_id: str) -> list[str]: ...
    async def get_results(self, run_id: str, agent_version: str) -> list[dict]: ...
    async def get_manifest(self, agent_id: str, agent_version: str) -> dict | None: ...


@dataclass
class RunSide:
    """One side of a comparison: a run narrowed to exactly one agent version."""

    run_id: str
    agent_version: str
    agent_id: str
    suite_id: str
    suite_name: str | None
    run_status: str | None
    version_status: str | None
    mode: str | None
    trials_per_case: int
    cache_mode: str | None = None
    git_sha: str | None = None
    versions_in_run: list[str] = field(default_factory=list)
    version_selection: str = "explicit"  # explicit | single_version_fallback

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> RunSide:
        return cls(**d)


def select_version(run_id: str, versions: list[str], requested: str | None) -> tuple[str, str]:
    """Pick one agent version of ``run_id``. ``requested`` may be the full hash or a unique
    prefix of at least 8 hex chars. Returns (version, how) where how is ``explicit`` or
    ``single_version_fallback``."""
    versions = sorted(set(versions))
    if not versions:
        raise ComparisonError(f"run {run_id} has no agent versions / results", "not_found")
    if requested is None:
        if len(versions) == 1:
            return versions[0], "single_version_fallback"
        short = ", ".join(v[:12] for v in versions)
        raise ComparisonError(
            f"run {run_id} contains {len(versions)} agent versions ({short}); select one "
            "explicitly — results are never averaged across versions"
        )
    if not re.fullmatch(r"[0-9a-f]+", requested) or len(requested) < _VERSION_PREFIX_MIN:
        raise ComparisonError(
            f"agent version must be >= {_VERSION_PREFIX_MIN} lowercase hex chars, got {requested!r}"
        )
    matches = [v for v in versions if v.startswith(requested)]
    if not matches:
        raise ComparisonError(f"agent version {requested} is not in run {run_id}", "not_found")
    if len(matches) > 1:
        raise ComparisonError(f"agent version prefix {requested} is ambiguous in run {run_id}")
    return matches[0], "explicit"


def canonical_run_id(run_id: str) -> str:
    """Canonical lowercase hyphenated UUID text. Postgres accepts other spellings (upper case,
    braces, no hyphens) of the same id, so identity checks must compare the canonical form."""
    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError as exc:
        raise ComparisonError(f"run id {run_id!r} is not a UUID") from exc


async def _resolve_side(store: TriageStore, run_id: str, requested: str | None) -> RunSide:
    run_id = canonical_run_id(run_id)
    meta = await store.get_run(run_id)
    if meta is None:
        raise ComparisonError(f"run {run_id} not found", "not_found")
    vrows = await store.get_run_versions(run_id)
    version, how = select_version(run_id, [v["agent_version"] for v in vrows], requested)
    vstatus = next((v.get("status") for v in vrows if v["agent_version"] == version), None)
    return RunSide(
        run_id=str(run_id),
        agent_version=version,
        agent_id=meta["agent_id"],
        suite_id=str(meta["suite_id"]),
        suite_name=meta.get("suite"),
        run_status=meta.get("status"),
        version_status=vstatus,
        mode=meta.get("mode"),
        trials_per_case=int(meta.get("trials_per_case") or 1),
        cache_mode=meta.get("cache_mode"),
        git_sha=meta.get("git_sha"),
        versions_in_run=sorted({v["agent_version"] for v in vrows}),
        version_selection=how,
    )


async def resolve_pair(
    store: TriageStore,
    baseline_run: str,
    candidate_run: str,
    baseline_version: str | None = None,
    candidate_version: str | None = None,
    *,
    require_same_suite: bool = True,
    allow_identical: bool = False,
) -> tuple[RunSide, RunSide]:
    b = await _resolve_side(store, baseline_run, baseline_version)
    c = await _resolve_side(store, candidate_run, candidate_version)
    if require_same_suite and b.suite_id != c.suite_id:
        raise ComparisonError(
            "runs are from different suites; a paired comparison requires the same suite"
        )
    if not allow_identical and (b.run_id, b.agent_version) == (c.run_id, c.agent_version):
        raise ComparisonError(
            "baseline and candidate resolve to the same run and agent version; select "
            "different versions to compare"
        )
    return b, c


# --- inputs (the exported raw data) --------------------------------------------------


def _num(v) -> float | None:
    return None if v is None else float(v)


def normalize_row(r: dict) -> dict:
    """A raw ``eval_case_results`` row → the JSON-native form compared and exported. Scorer
    entries are reduced to their numeric score (the same rule the CLI always used)."""
    scores = {}
    for name, entry in (r.get("scores") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("score"), (int, float)):
            scores[name] = float(entry["score"])
    err = r.get("error")
    return {
        "case_id": str(r["case_id"]),
        "trial": int(r["trial"]),
        "status": str(r["status"]),
        "passed": None if r.get("passed") is None else bool(r["passed"]),
        "scores": scores,
        "cost_usd": _num(r.get("cost_usd")),
        "latency_ms": _num(r.get("latency_ms")),
        "error": err[:_MAX_ERROR_CHARS] if isinstance(err, str) else None,
        "trace_id": r.get("trace_id"),
    }


@dataclass
class ComparisonInputs:
    baseline: RunSide
    candidate: RunSide
    suite_case_ids: list[str]
    baseline_rows: list[dict]
    candidate_rows: list[dict]

    def to_dict(self) -> dict:
        return {
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "suite_case_ids": list(self.suite_case_ids),
            "baseline_rows": self.baseline_rows,
            "candidate_rows": self.candidate_rows,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ComparisonInputs:
        return cls(
            baseline=RunSide.from_dict(d["baseline"]),
            candidate=RunSide.from_dict(d["candidate"]),
            suite_case_ids=list(d["suite_case_ids"]),
            baseline_rows=list(d["baseline_rows"]),
            candidate_rows=list(d["candidate_rows"]),
        )


def _sorted_rows(rows: list[dict]) -> list[dict]:
    return sorted((normalize_row(r) for r in rows), key=lambda r: (r["case_id"], r["trial"]))


async def load_inputs(
    store: TriageStore,
    baseline_run: str,
    candidate_run: str,
    baseline_version: str | None = None,
    candidate_version: str | None = None,
) -> ComparisonInputs:
    b, c = await resolve_pair(store, baseline_run, candidate_run, baseline_version,
                              candidate_version)
    return ComparisonInputs(
        baseline=b,
        candidate=c,
        suite_case_ids=sorted(await store.get_suite_case_ids(b.suite_id)),
        baseline_rows=_sorted_rows(await store.get_results(b.run_id, b.agent_version)),
        candidate_rows=_sorted_rows(await store.get_results(c.run_id, c.agent_version)),
    )


# --- series + comparison -------------------------------------------------------------


def direction_of(metric: str) -> str:
    return METRIC_DIRECTION.get(metric, HIGHER_IS_BETTER)


def quality_metrics(*row_sets: list[dict]) -> list[str]:
    """Higher-is-better metrics reported by any OK trial on either side — the set a failed
    trial is scored 0 on."""
    names = {"passed"}
    for rows in row_sets:
        for r in rows:
            if r["status"] == "ok":
                names.update(r["scores"])
    return [m for m in names if direction_of(m) == HIGHER_IS_BETTER and m not in _OK_ONLY_METRICS]


def build_series(rows: list[dict], quality: list[str]) -> dict[str, dict[str, list[float]]]:
    """{metric: {case_id: [per-trial values]}} under FAILED_TRIAL_POLICY."""
    series: dict[str, dict[str, list[float]]] = {}

    def add(metric, case_id, value):
        series.setdefault(metric, {}).setdefault(case_id, []).append(float(value))

    for r in rows:
        cid = r["case_id"]
        if r["status"] != "ok":
            for m in quality:
                add(m, cid, 0.0)
            continue
        if r["passed"] is not None:  # an OK trial without a verdict is unmeasured, not a fail
            add("passed", cid, 1.0 if r["passed"] else 0.0)
        for name, score in r["scores"].items():
            add(name, cid, score)
        if r["cost_usd"] is not None:
            add("cost_usd", cid, r["cost_usd"])
        if r["latency_ms"] is not None:
            add("latency_ms", cid, r["latency_ms"])
    return series


def _ok_trial_metrics(r: dict) -> set[str]:
    """Metrics an OK trial actually measured."""
    names = set(r["scores"])
    if r["passed"] is not None:
        names.add("passed")
    for m in _OK_ONLY_METRICS:
        if r[m] is not None:
            names.add(m)
    return names


def side_availability(rows: list[dict], series: dict) -> dict[str, dict]:
    """{metric: availability} for one side (see the module doc). ``measured``: reported by at
    least one OK trial. ``failure_zeros_only``: the side has no OK trials, so its series for
    this metric is failure zeros standing in for measurements. ``ok_trials_missing``: OK trials
    on this side that do not report a measured metric (partial coverage)."""
    ok = [r for r in rows if r["status"] == "ok"]
    per_trial = [_ok_trial_metrics(r) for r in ok]
    measured = set().union(*per_trial)
    out = {}
    for m in set(series) | measured:
        if m in measured:
            out[m] = {"measured": True, "failure_zeros_only": False,
                      "ok_trials_missing": sum(1 for t in per_trial if m not in t)}
        elif not ok:
            out[m] = {"measured": False, "failure_zeros_only": True, "ok_trials_missing": 0}
        else:  # zero-filled on failed trials only because the other side measures it
            out[m] = {"measured": False, "failure_zeros_only": False, "ok_trials_missing": 0}
    return out


def _usable(avail: dict | None) -> bool:
    return bool(avail) and (avail["measured"] or avail["failure_zeros_only"])


def ordered_metrics(names) -> list[str]:
    names = set(names)
    return [m for m in METRIC_ORDER if m in names] + sorted(names - set(METRIC_ORDER))


def _side_coverage(side: RunSide, rows: list[dict], suite: set[str]) -> dict:
    per_case = Counter(r["case_id"] for r in rows)
    statuses = Counter(r["status"] for r in rows)
    short = sorted(c for c, n in per_case.items() if n < side.trials_per_case)
    return {
        "cases": len(per_case),
        "trials": len(rows),
        "expected_trials": len(suite) * side.trials_per_case,
        "ok_trials": statuses.get("ok", 0),
        "failed_trials": len(rows) - statuses.get("ok", 0),
        "statuses": dict(sorted(statuses.items())),
        "missing_cases": sorted(suite - set(per_case)),
        "cases_short_of_trials": short,
        "cases_not_in_suite": sorted(set(per_case) - suite),
    }


@dataclass
class Comparison:
    baseline: RunSide
    candidate: RunSide
    policy: Policy
    verdict: str
    gated_regression: bool
    complete: bool
    incomplete_reasons: list[str]
    paired_cases: int
    coverage: dict
    metrics: list[dict]
    skipped_metrics: list[dict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "gated_regression": self.gated_regression,
            "complete": self.complete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "paired_cases": self.paired_cases,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "policy": self.policy.to_dict(),
            "coverage": self.coverage,
            "metrics": self.metrics,
            "skipped_metrics": self.skipped_metrics,
        }


def compare(inputs: ComparisonInputs, policy: Policy = DEFAULT_POLICY) -> Comparison:
    b, c = inputs.baseline, inputs.candidate
    suite = set(inputs.suite_case_ids)
    quality = quality_metrics(inputs.baseline_rows, inputs.candidate_rows)
    bs = build_series(inputs.baseline_rows, quality)
    cs = build_series(inputs.candidate_rows, quality)

    b_cases = {r["case_id"] for r in inputs.baseline_rows}
    c_cases = {r["case_id"] for r in inputs.candidate_rows}
    paired = b_cases & c_cases
    cov_b = _side_coverage(b, inputs.baseline_rows, suite)
    cov_c = _side_coverage(c, inputs.candidate_rows, suite)
    coverage = {
        "suite_cases": len(suite),
        "paired_cases": len(paired),
        "only_in_baseline": sorted(b_cases - c_cases),
        "only_in_candidate": sorted(c_cases - b_cases),
        "baseline": cov_b,
        "candidate": cov_c,
    }

    reasons: list[str] = []
    for label, side, cov in (("baseline", b, cov_b), ("candidate", c, cov_c)):
        if side.run_status != "succeeded":
            reasons.append(f"{label} run status is {side.run_status!r}, not 'succeeded'")
        if side.version_status not in (None, "succeeded"):
            reasons.append(f"{label} version status is {side.version_status!r}")
        if not cov["trials"]:
            reasons.append(f"{label} has no results for version {side.agent_version[:12]}")
        elif not cov["ok_trials"]:
            reasons.append(
                f"{label} has no OK trials (all {cov['trials']} failed); its quality metrics are "
                "failure zeros, not measurements"
            )
        if cov["missing_cases"]:
            reasons.append(f"{label} is missing {len(cov['missing_cases'])} suite case(s)")
        if cov["cases_short_of_trials"]:
            reasons.append(
                f"{label} has {len(cov['cases_short_of_trials'])} case(s) with fewer than "
                f"{side.trials_per_case} trial(s)"
            )
        if cov["cases_not_in_suite"]:
            reasons.append(f"{label} has {len(cov['cases_not_in_suite'])} case(s) not in suite")
    if coverage["only_in_baseline"] or coverage["only_in_candidate"]:
        reasons.append(
            f"unequal case sets: {len(coverage['only_in_baseline'])} only in baseline, "
            f"{len(coverage['only_in_candidate'])} only in candidate (paired on the intersection)"
        )

    av_b = side_availability(inputs.baseline_rows, bs)
    av_c = side_availability(inputs.candidate_rows, cs)
    metrics, skipped = [], []
    for m in ordered_metrics(set(bs) | set(cs) | set(policy.gated)):
        gated = m in policy.gated
        if m not in bs and m not in cs:
            continue  # gated metric this suite does not report on either side: not applicable
        use_b, use_c = _usable(av_b.get(m)), _usable(av_c.get(m))
        if not (use_b and use_c):
            if use_b or use_c:
                reason = f"only reported by {'baseline' if use_b else 'candidate'}"
            else:  # e.g. OK trials without a pass/fail verdict, plus some failed trials
                reason = "not measured on any OK trial of either side"
            skipped.append({"metric": m, "gated": gated, "reason": reason})
            continue
        missing = {"baseline": av_b[m]["ok_trials_missing"],
                   "candidate": av_c[m]["ok_trials_missing"]}
        failure_zeros = [s for s, a in (("baseline", av_b[m]), ("candidate", av_c[m]))
                         if a["failure_zeros_only"]]
        if gated:
            for label, n in missing.items():
                if n:
                    reasons.append(f"gated metric {m} is missing on {n} OK trial(s) of {label}")
        try:
            v = paired_bootstrap_diff(
                bs[m], cs[m], metric=m, direction=direction_of(m),
                threshold=policy.threshold(m), alpha=policy.alpha, n_boot=policy.n_boot,
                seed=policy.seed, two_level=policy.two_level, method=policy.method,
            )
        except ValueError as exc:
            skipped.append({"metric": m, "gated": gated, "reason": str(exc)})
            continue
        metrics.append({
            "metric": m,
            "gated": gated,
            "threshold": float(v.threshold),
            "point_estimate": float(v.point_estimate),
            "ci_bound": float(v.ci_bound),
            "ci_excludes_zero": bool(v.ci_excludes_zero),
            "beyond_threshold": bool(v.beyond_threshold),
            "regressed": bool(v.regressed),
            "direction": v.direction,
            "tail_prob": float(v.tail_prob),
            "n_questions": v.n_questions,
            "status": "regression" if v.regressed else ("watch" if v.ci_excludes_zero else "ok"),
            # Availability audit: OK trials per side that lack this metric, and sides whose
            # series is failure zeros only (no OK trials there).
            "ok_trials_missing": missing,
            "failure_zeros_only": failure_zeros,
        })
    for s in skipped:
        if s["gated"]:
            reasons.append(f"gated metric {s['metric']} not compared: {s['reason']}")

    gated_regression = any(r["regressed"] for r in metrics if r["gated"])
    complete = not reasons
    verdict = "regression" if gated_regression else ("no_regression" if complete else "incomplete")
    return Comparison(
        baseline=b, candidate=c, policy=policy, verdict=verdict,
        gated_regression=gated_regression, complete=complete, incomplete_reasons=reasons,
        paired_cases=len(paired), coverage=coverage, metrics=metrics, skipped_metrics=skipped,
    )


# --- per-case deltas (triage drill-down) ---------------------------------------------


def _mean(xs: list[float] | None) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _case_side(rows: list[dict], series: dict, avail: dict, metrics: list[str]) -> dict:
    statuses = Counter(r["status"] for r in rows)
    cid = rows[0]["case_id"] if rows else None
    return {
        "n_trials": len(rows),
        "n_ok": statuses.get("ok", 0),
        "n_failed": len(rows) - statuses.get("ok", 0),
        "statuses": dict(sorted(statuses.items())),
        # None where this side never measured the metric (no invented failure-zero means).
        "mean": {m: _mean(series.get(m, {}).get(cid)) if _usable(avail.get(m)) else None
                 for m in metrics},
        "trials": [
            {"trial": r["trial"], "status": r["status"], "passed": r["passed"],
             "trace_id": r["trace_id"], "error": r["error"]}
            for r in rows
        ],
    }


def _series_and_availability(inputs: ComparisonInputs):
    quality = quality_metrics(inputs.baseline_rows, inputs.candidate_rows)
    bs = build_series(inputs.baseline_rows, quality)
    cs = build_series(inputs.candidate_rows, quality)
    av_b = side_availability(inputs.baseline_rows, bs)
    av_c = side_availability(inputs.candidate_rows, cs)
    metrics = ordered_metrics(
        m for m in set(bs) | set(cs) if _usable(av_b.get(m)) or _usable(av_c.get(m))
    )
    return bs, cs, av_b, av_c, metrics


def available_metrics(inputs: ComparisonInputs) -> list[str]:
    """Metrics usable on at least one side (empty when neither side has results)."""
    return _series_and_availability(inputs)[4]


def case_deltas(inputs: ComparisonInputs, sort_metric: str) -> list[dict]:
    """Per-case side-by-side for every case either side ran, sorted most-adverse first on
    ``sort_metric``: paired cases by adverse delta (descending), then unpaired cases (one side
    has no trials at all) by case_id. ``adverse_delta`` > 0 means the candidate is worse.
    No results on either side → no cases (not an unknown-metric error)."""
    if not inputs.baseline_rows and not inputs.candidate_rows:
        return []
    bs, cs, av_b, av_c, metrics = _series_and_availability(inputs)
    if sort_metric not in metrics:
        raise ComparisonError(f"unknown metric {sort_metric!r}; available: {metrics}")
    sign = 1.0 if direction_of(sort_metric) == LOWER_IS_BETTER else -1.0

    by_case_b: dict[str, list[dict]] = {}
    by_case_c: dict[str, list[dict]] = {}
    for r in inputs.baseline_rows:
        by_case_b.setdefault(r["case_id"], []).append(r)
    for r in inputs.candidate_rows:
        by_case_c.setdefault(r["case_id"], []).append(r)

    out = []
    for cid in sorted(set(by_case_b) | set(by_case_c)):
        b = _case_side(by_case_b.get(cid, []), bs, av_b, metrics)
        c = _case_side(by_case_c.get(cid, []), cs, av_c, metrics)
        delta = {
            m: (c["mean"][m] - b["mean"][m])
            if b["mean"][m] is not None and c["mean"][m] is not None else None
            for m in metrics
        }
        d = delta[sort_metric]
        out.append({
            "case_id": cid,
            "paired": cid in by_case_b and cid in by_case_c,
            "delta": delta,
            "adverse_delta": sign * d if d is not None else None,
            "baseline": b,
            "candidate": c,
        })
    out.sort(key=lambda r: (r["adverse_delta"] is None, -(r["adverse_delta"] or 0.0),
                            r["case_id"]))
    return out


def json_safe(obj: Any) -> Any:
    """Replace non-finite floats (nan/inf from degenerate CIs) by None for strict JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
