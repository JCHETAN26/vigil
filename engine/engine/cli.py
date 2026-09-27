"""Engine CLI (design §10): ``python -m engine <command>``.

python -m engine migrate
python -m engine suite create --name calc --adapter local --path suites/calc.json
python -m engine run --suite calc --agent hello_agent.agent --mode development --trials 2
python -m engine show --run <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys

import psycopg

from engine.config import EngineConfig, pg_dsn
from engine.datasets.base import get_adapter
from engine.db.migrate import main as migrate_main
from engine.db.repo import CaseRow, Repo
from engine.runner.cache import CacheRefusedError
from engine.runner.orchestrator import Orchestrator, WorkerCrashed


def _connect() -> psycopg.Connection:
    return psycopg.connect(pg_dsn())


def cmd_migrate(_args) -> int:
    return migrate_main()


def cmd_suite_create(args) -> int:
    adapter_cls = get_adapter(args.adapter)
    config = {"path": args.path}
    if args.subset_path:
        config["subset_path"] = args.subset_path
    if args.split:
        config["split"] = args.split
    if args.version:
        config["version"] = args.version
    adapter = adapter_cls.from_config(config)
    config["version"] = adapter.version  # pin the resolved version for reproducibility
    cases = list(adapter.load())

    with _connect() as conn:
        repo = Repo(conn)
        if repo.get_suite_by_name(args.name):
            print(f"suite {args.name!r} already exists", file=sys.stderr)
            return 1
        suite = repo.create_suite(name=args.name, adapter=args.adapter, config=config)
        repo.add_cases(
            [
                CaseRow(
                    suite_id=suite.id,
                    case_id=c.case_id,
                    input=c.input,
                    expected=c.expected,
                    tags=c.tags,
                )
                for c in cases
            ]
        )
        conn.commit()
    print(
        f"created suite {args.name!r} ({adapter.name} v{adapter.version}) with {len(cases)} case(s)"
    )
    return 0


def _check_free_disk() -> None:
    """Refuse to start an eval run when free disk is under the floor (default 1 GB). An eval run
    writes traces to ClickHouse and results to Postgres; running the disk out mid-run corrupts
    state and can affect other work on the machine. Override the floor with
    VIGIL_MIN_FREE_DISK_GB (0 disables)."""
    floor_gb = float(os.getenv("VIGIL_MIN_FREE_DISK_GB", "1.0"))
    if floor_gb <= 0:
        return
    free_gb = shutil.disk_usage(os.getcwd()).free / 1e9
    if free_gb < floor_gb:
        raise SystemExit(
            f"error: refusing to start eval run — only {free_gb:.2f} GB free, need >= "
            f"{floor_gb:.2f} GB (free space or set VIGIL_MIN_FREE_DISK_GB). "
            "Vigil never deletes on its own."
        )
    print(f"disk check: {free_gb:.2f} GB free (floor {floor_gb:.2f} GB) — ok")


def cmd_run(args) -> int:
    _check_free_disk()
    cfg = EngineConfig.from_env()
    with _connect() as conn:
        orch = Orchestrator(conn, cfg)
        result = asyncio.run(
            orch.run(
                suite_name=args.suite,
                agent_modules=args.agent,
                mode=args.mode,
                cost_budget_usd=args.budget,
                concurrency=args.concurrency,
                trials_per_case=args.trials,
                cache_mode=args.cache,
                notes=args.notes,
            )
        )
    print(
        f"run {result['run_id']} {result['status']}"
        + (f" (aborted: {result['abort_reason']})" if result["aborted"] else "")
        + f" — {result['completed']} unit(s), ${result['cost']:.4f}"
    )
    return 0


def cmd_show(args) -> int:
    with _connect() as conn:
        conn.execute("SELECT 1")  # ensure the connection is live
        row = conn.execute(
            "SELECT status, mode, cost_spent_usd, cost_budget_usd FROM eval_runs WHERE id = %s",
            (args.run,),
        ).fetchone()
        if row is None:
            print(f"no run {args.run}", file=sys.stderr)
            return 1
        repo = Repo(conn)
        print(
            f"run {args.run}: status={row[0]} mode={row[1]} spent=${float(row[2]):.4f}"
            + (f" budget=${float(row[3]):.4f}" if row[3] is not None else " budget=none")
        )
        for v in repo.get_run_versions(args.run):
            print(
                f"  version {v['agent_version'][:12]}: {v['status']} "
                f"{v['cases_done']}/{v['cases_total']} ${float(v['cost_spent_usd']):.4f}"
            )
        results = repo.get_case_results(args.run)
        passed = sum(1 for r in results if r["passed"])
        print(f"  results: {len(results)} total, {passed} passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("engine")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply Postgres migrations").set_defaults(func=cmd_migrate)

    sc = sub.add_parser("suite", help="suite management")
    scsub = sc.add_subparsers(dest="subcommand", required=True)
    create = scsub.add_parser("create", help="materialize a suite from an adapter")
    create.add_argument("--name", required=True)
    create.add_argument("--adapter", required=True, help="adapter name, e.g. 'local'")
    create.add_argument("--path", required=True, help="dataset path (adapter-specific)")
    create.add_argument(
        "--subset-path",
        default=None,
        help="file of case ids (one per line) to restrict the suite to (adapter-specific)",
    )
    create.add_argument(
        "--split",
        default=None,
        help="adapter-specific split to materialize (e.g. tau2_retail: 'dev' | 'measurement')",
    )
    create.add_argument("--version", default=None)
    create.set_defaults(func=cmd_suite_create)

    run = sub.add_parser("run", help="run an agent over a suite")
    run.add_argument("--suite", required=True)
    run.add_argument(
        "--agent", action="append", required=True, help="agent module path (repeatable)"
    )
    run.add_argument("--mode", choices=["measurement", "development"], default="development")
    run.add_argument("--budget", type=float, default=None, help="per-run cost budget in USD")
    run.add_argument("--concurrency", type=int, default=None)
    run.add_argument("--trials", type=int, default=None)
    run.add_argument("--cache", choices=["off", "read", "read_write"], default=None)
    run.add_argument("--notes", default=None)
    run.set_defaults(func=cmd_run)

    show = sub.add_parser("show", help="show a run's results")
    show.add_argument("--run", required=True)
    show.set_defaults(func=cmd_show)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return args.func(args)
    except (CacheRefusedError, WorkerCrashed, KeyError, ValueError) as exc:
        # Expected, actionable failures: a clear one-line message, not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
