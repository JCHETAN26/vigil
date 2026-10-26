"""``make demo``: seed the SYNTHETIC fixture, check it, then run the real read-only API and the
dashboard on free loopback ports and print where to click. Ctrl-C stops only what this started.

    python demo/launch.py [--api-port 8091] [--ui-port 3318] [--no-seed] [--direct-api]

Prerequisites (checked first; nothing is installed, migrated, created or deleted here):
the stack up with Postgres migrated (``python -m engine migrate``) and ClickHouse migrated (the
ingest service), the read-only roles created (``deploy/create_readonly_users.sh``), admin and RO
credentials in the environment (the repo .env), ``engine/.venv`` and ``dashboard/node_modules``.

What it starts, both bound to 127.0.0.1 and in their own process groups:
- the API (``python -m engine.api``) with the read-only credentials only: the admin passwords and
  every model API key are removed from its environment;
- the dashboard dev server (``next dev``, build dir ``.next-demo``) with the demo notice on and,
  by default, ``NEXT_PUBLIC_API_URL=/api/vigil``: the browser talks only to the UI port and the
  dashboard's server-side proxy forwards to this API (so one SSH forward is enough).
  ``--direct-api`` makes the browser call the API port directly instead (CORS allows the UI).

An occupied port, a missing prerequisite, a failed seed/check or a child that exits early stops
the launcher with a clear message; it never kills a process it did not start and never falls
back to another port or app.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
import seed as seeding
import synthetic_fixture as sf
from engine.config import pg_dsn, pg_ro_dsn, uses_ro_pg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_PORT = 8091
DEFAULT_UI_PORT = 3318
PILOT_RUN = "cafae810-f2e9-4c50-b34d-cfac8b18c509"
PILOT_VERSIONS = ("c2eda30a02146432a38387ed183420243d6752683d87856ddd8cb38e447d5b4c",
                  "4b6ff74526af186f0df0017aa96d9d34e3c6b86a476c8aa39f85d2b95d2d3949")
NEXT_BIN = ROOT / "dashboard" / "node_modules" / ".bin" / "next"
# Model-provider credentials never reach the demo processes (nothing in the demo calls a model).
_MODEL_KEY = re.compile(r"^(ANTHROPIC_|OPENAI_|CLAUDE_|GEMINI_|GOOGLE_API_KEY$)|_API_KEY$")
# The API reads with the RO roles only; the UI needs no database credentials at all.
_ADMIN_VARS = ("POSTGRES_PASSWORD", "VIGIL_PG_DSN", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD",
               "VIGIL_REDIS_URL")
_DB_VARS = _ADMIN_VARS + ("POSTGRES_RO_PASSWORD", "VIGIL_API_PG_DSN", "CLICKHOUSE_RO_PASSWORD")


class LaunchError(RuntimeError):
    pass


def clean_env(drop: tuple[str, ...] = ()) -> dict[str, str]:
    """This environment minus model keys, ``drop`` and PYTHONPATH (the venv stays isolated)."""
    return {k: v for k, v in os.environ.items()
            if not _MODEL_KEY.search(k) and k not in drop and k != "PYTHONPATH"}


def port_free(port: int) -> bool:
    """True if nothing listens on 127.0.0.1:port and it can be bound there."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def preflight(api_port: int, ui_port: int, need_admin: bool) -> list[str]:
    problems = []
    if api_port == ui_port:
        problems.append("--api-port and --ui-port must differ")
    for name, port in (("API", api_port), ("dashboard", ui_port)):
        if not 1024 <= port <= 65535:
            problems.append(f"{name} port {port} is outside 1024-65535")
        elif not port_free(port):
            problems.append(f"{name} port 127.0.0.1:{port} is in use; pick another with "
                            f"{'DEMO_API_PORT' if name == 'API' else 'DEMO_UI_PORT'}=... "
                            "(nothing was stopped)")
    if not NEXT_BIN.exists():
        problems.append("dashboard/node_modules is missing: run `npm ci` in dashboard/")
    if not uses_ro_pg():
        problems.append("no read-only Postgres role configured (POSTGRES_RO_USER/"
                        "POSTGRES_RO_PASSWORD); create it with deploy/create_readonly_users.sh")
    if not (os.getenv("CLICKHOUSE_RO_USER") and os.getenv("CLICKHOUSE_RO_PASSWORD")):
        problems.append("no read-only ClickHouse user configured (CLICKHOUSE_RO_USER/"
                        "CLICKHOUSE_RO_PASSWORD); create it with deploy/create_readonly_users.sh")
    if need_admin:
        if not (os.getenv("VIGIL_PG_DSN") or os.getenv("POSTGRES_PASSWORD")):
            problems.append("seeding needs the Postgres admin credentials (POSTGRES_PASSWORD or "
                            "VIGIL_PG_DSN); or pass --no-seed if the fixture is already seeded")
        if not os.getenv("CLICKHOUSE_PASSWORD"):
            problems.append("seeding needs the ClickHouse admin password (CLICKHOUSE_PASSWORD)")
    return problems


def seed_and_check(do_seed: bool) -> bool:
    """Seed (idempotent) and check; returns whether the real pilot run is in this database."""
    if do_seed:
        with psycopg.connect(pg_dsn(), connect_timeout=5) as conn:
            done = seeding.seed(conn, seeding.ClickHouseSpans.admin_from_env())
        for store, what in done.items():
            print(f"  seed {store}: {what}")
    dsn = pg_ro_dsn()
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        problems = seeding.check(conn, seeding.ClickHouseSpans.readonly_from_env(), dsn)
        if problems:
            raise LaunchError("demo check failed: " + "; ".join(problems))
        cur = conn.execute("SELECT 1 FROM eval_runs WHERE id = %s", (PILOT_RUN,))
        return cur.fetchone() is not None


def _get(url: str, timeout: float = 3.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


def wait_until(url: str, procs: list[subprocess.Popen], deadline_s: float) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        for p in procs:
            if p.poll() is not None:
                raise LaunchError(f"{p.args[0]} exited early with status {p.returncode}")
        if _get(url) == 200:
            return
        time.sleep(1.0)
    raise LaunchError(f"{url} did not answer 200 within {deadline_s:.0f}s")


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:  # the group is empty: nothing left to stop
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # members exist but are not ours to signal
        return True
    return True


def stop(procs: list[subprocess.Popen], grace_s: float = 10.0) -> None:
    """Terminate the process groups this launcher started, and nothing else.

    Each child was started with ``start_new_session=True``, so its pid is the id of a process
    group holding it and everything it spawned (e.g. the Next server under ``next dev``). The
    whole group is signalled even when its leader has already exited: a surviving descendant
    would otherwise keep the port. The leader is reaped first (``poll``), because an unreaped
    zombie leader would keep the group looking alive. The kernel does not hand out a pid that
    is still a live group's id, so a group id is reused only after the group is empty."""
    for p in procs:
        p.poll()
        _signal_group(p.pid, signal.SIGTERM)
    end = time.monotonic() + grace_s
    for p in procs:
        while (p.poll() is None or _group_alive(p.pid)) and time.monotonic() < end:
            time.sleep(0.1)
    for p in procs:
        if p.poll() is None or _group_alive(p.pid):
            _signal_group(p.pid, signal.SIGKILL)
        p.wait()


def links(api_port: int, ui_port: int, pilot_present: bool, proxy: bool) -> str:
    ui = f"http://127.0.0.1:{ui_port}"
    fx = sf.build()
    lines = [
        "",
        "Vigil demo is up (local only; read-only API without authentication; not a public "
        "service).",
        f"  Dashboard     {ui}/runs",
        f"  API           http://127.0.0.1:{api_port}  (browser uses "
        f"{'the dashboard proxy /api/vigil' if proxy else 'this port directly'})",
        "",
        "SYNTHETIC prepared examples (invented data, no model calls; verdicts computed by "
        "engine.gate from the seeded rows):",
    ]
    for scn in fx.scenarios:
        lines.append(f"  {scn.expected_verdict:14s} {ui}{seeding.compare_path(scn)}")
    lines.append(f"  trace of an affected case: {ui}/traces/{seeding.first_affected_trace(fx)}")
    if pilot_present:
        lines += ["", "REAL exploratory HotpotQA pilot (present in this database):",
                  f"  {ui}/compare?baseline={PILOT_RUN}&baseline_version={PILOT_VERSIONS[0]}"
                  f"&candidate={PILOT_RUN}&candidate_version={PILOT_VERSIONS[1]}"]
    lines += [
        "",
        "Offline reproduction of the saved REAL pilot verdict (no DB, no key): make demo-evidence",
        f"From another machine: ssh -N -L {ui_port}:127.0.0.1:{ui_port} <ssh-host>   then open "
        f"http://127.0.0.1:{ui_port}/runs"
        + ("" if proxy else f"  (with --direct-api also forward {api_port})"),
        "Ctrl-C stops the API and dashboard started here.",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("demo/launch.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--api-port", type=int, default=int(os.getenv("DEMO_API_PORT",
                                                                   DEFAULT_API_PORT)))
    ap.add_argument("--ui-port", type=int, default=int(os.getenv("DEMO_UI_PORT", DEFAULT_UI_PORT)))
    ap.add_argument("--no-seed", action="store_true", help="only check an already seeded fixture")
    ap.add_argument("--direct-api", action="store_true",
                    help="browser calls the API port directly instead of the dashboard proxy")
    ap.add_argument("--startup-timeout", type=float, default=240.0)
    args = ap.parse_args(argv)

    problems = preflight(args.api_port, args.ui_port, need_admin=not args.no_seed)
    if problems:
        print("demo: cannot start:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    procs: list[subprocess.Popen] = []
    stopping = {"signal": None}

    def on_signal(signum, _frame):
        stopping["signal"] = signum
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    try:
        print("demo: seeding and checking the SYNTHETIC fixture...")
        pilot_present = seed_and_check(not args.no_seed)
        api_env = clean_env(_ADMIN_VARS)
        api_env.update(VIGIL_API_PORT=str(args.api_port), VIGIL_DASHBOARD_ORIGINS=(
            f"http://127.0.0.1:{args.ui_port},http://localhost:{args.ui_port}"))
        procs.append(subprocess.Popen([sys.executable, "-m", "engine.api"], cwd=ROOT / "engine",
                                      env=api_env, start_new_session=True))
        ui_env = clean_env(_DB_VARS)
        ui_env.update(
            NEXT_PUBLIC_API_URL="/api/vigil" if not args.direct_api
            else f"http://127.0.0.1:{args.api_port}",
            VIGIL_API_PROXY_TARGET=f"http://127.0.0.1:{args.api_port}",
            NEXT_PUBLIC_VIGIL_DEMO="1", NEXT_DIST_DIR=".next-demo", NEXT_TELEMETRY_DISABLED="1",
            NODE_OPTIONS="--max-old-space-size=1536")
        procs.append(subprocess.Popen(
            [str(NEXT_BIN), "dev", "-H", "127.0.0.1", "-p", str(args.ui_port)],
            cwd=ROOT / "dashboard", env=ui_env, start_new_session=True))
        print(f"demo: started API (pid {procs[0].pid}) and dashboard (pid {procs[1].pid}); "
              "waiting for both to answer (the first dashboard compile can take a minute)...")
        wait_until(f"http://127.0.0.1:{args.api_port}/healthz", procs, args.startup_timeout)
        wait_until(f"http://127.0.0.1:{args.ui_port}/runs", procs, args.startup_timeout)
        if not args.direct_api:
            wait_until(f"http://127.0.0.1:{args.ui_port}/api/vigil/healthz", procs, 60)
        print(links(args.api_port, args.ui_port, pilot_present, proxy=not args.direct_api),
              flush=True)
        while True:
            for p in procs:
                if p.poll() is not None:
                    raise LaunchError(f"{p.args[0]} exited with status {p.returncode}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\ndemo: stopping the API and dashboard started here...")
        return 0
    except (RuntimeError, ValueError, psycopg.Error) as exc:  # incl. LaunchError, SeedError
        print(f"demo: {seeding.redact(str(exc))}", file=sys.stderr)
        return 1
    finally:
        stop(procs)


if __name__ == "__main__":
    raise SystemExit(main())
