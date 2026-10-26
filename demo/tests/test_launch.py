"""The demo launcher's guards, offline: it refuses occupied ports and missing prerequisites
before touching anything, strips model keys and admin credentials from what it starts, and stops
only the process groups it started. No stack, no browser."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launch


@pytest.fixture
def ro_env(monkeypatch):
    for k, v in {"POSTGRES_RO_USER": "ro", "POSTGRES_RO_PASSWORD": "ro-pw",
                 "CLICKHOUSE_RO_USER": "ro", "CLICKHOUSE_RO_PASSWORD": "ro-pw",
                 "POSTGRES_PASSWORD": "admin-pw", "CLICKHOUSE_PASSWORD": "admin-pw"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("VIGIL_API_PG_DSN", raising=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_occupied_port_is_refused_and_left_alone(ro_env):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert not launch.port_free(port)
        problems = launch.preflight(port, _free_port(), need_admin=True)
        assert any(f"127.0.0.1:{port} is in use" in p and "nothing was stopped" in p
                   for p in problems)
        # The listener is still ours and still accepting: nothing was killed or taken over.
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass


def test_same_port_twice_and_missing_credentials_are_refused(monkeypatch):
    for k in ("POSTGRES_RO_USER", "POSTGRES_RO_PASSWORD", "VIGIL_API_PG_DSN", "CLICKHOUSE_RO_USER",
              "CLICKHOUSE_RO_PASSWORD", "POSTGRES_PASSWORD", "VIGIL_PG_DSN",
              "CLICKHOUSE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    port = _free_port()
    problems = " | ".join(launch.preflight(port, port, need_admin=True))
    for expected in ("must differ", "read-only Postgres role", "read-only ClickHouse user",
                     "Postgres admin credentials", "ClickHouse admin password"):
        assert expected in problems
    # --no-seed does not need the admin credentials.
    assert "admin" not in " ".join(launch.preflight(_free_port(), _free_port(), need_admin=False))


def test_started_processes_get_no_model_keys_or_admin_credentials(monkeypatch, ro_env):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SOME_SERVICE_API_KEY", "PYTHONPATH"):
        monkeypatch.setenv(k, "x")
    api = launch.clean_env(launch._ADMIN_VARS)
    ui = launch.clean_env(launch._DB_VARS)
    for env in (api, ui):
        assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SOME_SERVICE_API_KEY",
                    "PYTHONPATH", "POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD"} & set(env)
    assert api["POSTGRES_RO_PASSWORD"] == "ro-pw"  # the API reads with the RO role only
    assert "POSTGRES_RO_PASSWORD" not in ui and "CLICKHOUSE_RO_PASSWORD" not in ui


def test_stop_terminates_only_the_groups_it_started():
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    try:
        launch.stop([ours])
        assert ours.poll() is not None
        assert other.poll() is None  # not started by this launcher: untouched
    finally:
        other.kill()
        other.wait()


def _gone(pid: int, timeout: float = 5.0) -> bool:
    """True once ``pid`` no longer exists or is only a zombie awaiting its (new) parent."""
    end = time.monotonic() + timeout
    while True:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state == "Z":
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to observe the grandchild")
def test_stop_cleans_up_a_surviving_descendant_after_its_leader_exited():
    # `next dev` style: the launched leader spawns a server into its own group, then the leader
    # exits. The server must still be stopped (it would hold the port), and an unrelated
    # process in another group must not be touched.
    spawn = ("import subprocess, sys; "
             "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
             "print(c.pid, flush=True)")
    leader = subprocess.Popen([sys.executable, "-c", spawn], stdout=subprocess.PIPE, text=True,
                              start_new_session=True)
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True)
    child = None
    try:
        child = int(leader.stdout.readline())
        leader.wait(timeout=10)  # the leader is gone; its child lives on in the same group
        assert not _gone(child, timeout=0.5)
        launch.stop([leader], grace_s=5)
        assert _gone(child)
        assert sentinel.poll() is None  # not in a group this launcher started
    finally:
        leader.stdout.close()
        if child is not None and not _gone(child, timeout=0):
            os.kill(child, signal.SIGKILL)  # only on failure: never leak the test's own child
        sentinel.kill()
        sentinel.wait()


def test_links_label_synthetic_examples_and_never_print_secrets(ro_env):
    text = launch.links(8091, 3318, pilot_present=False, proxy=True)
    assert "SYNTHETIC prepared examples" in text and "no model calls" in text
    assert text.count("http://127.0.0.1:3318/compare?") == 3
    assert "/traces/" in text and "make demo-evidence" in text
    assert "ssh -N -L 3318:127.0.0.1:3318 <ssh-host>" in text
    assert "REAL exploratory" not in text  # the pilot link only when that run is present
    assert "REAL exploratory HotpotQA pilot" in launch.links(8091, 3318, True, True)
    assert "pw" not in text
