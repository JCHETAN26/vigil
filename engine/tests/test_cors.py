"""CORS configuration of the dashboard API: VIGIL_DASHBOARD_ORIGINS parsing/validation and the
resulting browser-facing headers (configured origin and preflight allowed, unlisted origin gets
no access header, GET only, never credentialed). DB-free."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.api.app import add_cors
from engine.config import DEFAULT_DASHBOARD_ORIGINS, dashboard_origins

UI = "http://127.0.0.1:3307"
ACAO = "access-control-allow-origin"


def _client(monkeypatch, value: str | None) -> TestClient:
    if value is None:
        monkeypatch.delenv("VIGIL_DASHBOARD_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("VIGIL_DASHBOARD_ORIGINS", value)
    probe = FastAPI()
    probe.get("/healthz")(lambda: {"ok": True})
    add_cors(probe, dashboard_origins())
    return TestClient(probe)


def _preflight(client: TestClient, origin: str, method: str = "GET"):
    return client.options(
        "/healthz",
        headers={"Origin": origin, "Access-Control-Request-Method": method},
    )


# --- parsing ---


@pytest.mark.parametrize("value", [None, "", "   "])
def test_defaults_when_unset_or_blank(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("VIGIL_DASHBOARD_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("VIGIL_DASHBOARD_ORIGINS", value)
    assert dashboard_origins() == list(DEFAULT_DASHBOARD_ORIGINS)
    assert DEFAULT_DASHBOARD_ORIGINS == ("http://127.0.0.1:3000", "http://localhost:3000")


def test_configured_list_is_trimmed_and_deduplicated(monkeypatch):
    monkeypatch.setenv(
        "VIGIL_DASHBOARD_ORIGINS",
        f" {UI} ,http://localhost:3307,{UI},https://vigil.example.test,http://[::1]:3307",
    )
    assert dashboard_origins() == [
        UI, "http://localhost:3307", "https://vigil.example.test", "http://[::1]:3307",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "http://*.example.test",
        f"{UI}/",
        f"{UI}/dashboard",
        f"{UI}?x=1",
        f"{UI}#frag",
        "127.0.0.1:3307",
        "ftp://127.0.0.1:3307",
        "null",
        "http://",
        "http://user:pw@127.0.0.1:3307",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:abc",
        "http://127.0.0.1:0",
        "http://LOCALHOST:3307",
        "http://127.0.0.1:80",
        "https://vigil.example.test:443",
        f"{UI},,http://localhost:3307",
        f"{UI},",
    ],
)
def test_malformed_origins_fail_clearly(monkeypatch, value):
    monkeypatch.setenv("VIGIL_DASHBOARD_ORIGINS", value)
    with pytest.raises(ValueError, match="VIGIL_DASHBOARD_ORIGINS"):
        dashboard_origins()


# --- headers ---


def test_configured_origin_simple_get_and_preflight(monkeypatch):
    client = _client(monkeypatch, f"{UI},http://localhost:3307")
    r = client.get("/healthz", headers={"Origin": UI})
    assert r.status_code == 200 and r.headers[ACAO] == UI
    assert "access-control-allow-credentials" not in r.headers

    pre = _preflight(client, UI)
    assert pre.status_code == 200
    assert pre.headers[ACAO] == UI
    assert pre.headers["access-control-allow-methods"] == "GET"
    assert "access-control-allow-credentials" not in pre.headers


def test_unlisted_origin_gets_no_access_header(monkeypatch):
    client = _client(monkeypatch, UI)
    for origin in ("http://127.0.0.1:3000", "http://localhost:3307", "http://evil.example.test"):
        r = client.get("/healthz", headers={"Origin": origin})
        assert r.status_code == 200 and ACAO not in r.headers  # browser blocks the read
        pre = _preflight(client, origin)
        assert pre.status_code == 400 and ACAO not in pre.headers


def test_non_get_preflight_is_rejected(monkeypatch):
    client = _client(monkeypatch, UI)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert _preflight(client, UI, method).status_code == 400


def test_defaults_allow_localhost_3000_only(monkeypatch):
    client = _client(monkeypatch, None)
    for origin in DEFAULT_DASHBOARD_ORIGINS:
        assert _preflight(client, origin).headers[ACAO] == origin
    assert ACAO not in client.get("/healthz", headers={"Origin": UI}).headers


# --- the real app reads the variable at import (startup) ---


def _run_app(value: str, code: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["VIGIL_DASHBOARD_ORIGINS"] = value
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_real_app_uses_configured_origins():
    proc = _run_app(UI, f"""
        from fastapi.testclient import TestClient
        from engine.api.app import app
        c = TestClient(app)  # no context manager: the Postgres lifespan never runs
        pre = c.options("/compare", headers={{"Origin": "{UI}",
                                              "Access-Control-Request-Method": "GET"}})
        assert pre.status_code == 200, pre.status_code
        assert pre.headers["{ACAO}"] == "{UI}"
        other = c.get("/healthz", headers={{"Origin": "http://127.0.0.1:3000"}})
        assert other.status_code == 200 and "{ACAO}" not in other.headers
        print("ok")
    """)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_real_app_refuses_to_start_on_malformed_origins():
    proc = _run_app("*", "import engine.api.app")
    assert proc.returncode != 0
    assert "VIGIL_DASHBOARD_ORIGINS entry '*'" in proc.stderr
    assert "wildcards are not allowed" in proc.stderr
