"""The eval-run disk guard (design: refuse to start a run when free disk is under the floor)."""

from __future__ import annotations

import pytest

from engine.cli import _check_free_disk


def test_disk_check_disabled_with_zero_floor(monkeypatch):
    monkeypatch.setenv("VIGIL_MIN_FREE_DISK_GB", "0")
    _check_free_disk()  # no raise, no check


def test_disk_check_passes_with_tiny_floor(monkeypatch):
    monkeypatch.setenv("VIGIL_MIN_FREE_DISK_GB", "0.000001")
    _check_free_disk()  # any real disk clears a 1 KB floor


def test_disk_check_refuses_when_below_floor(monkeypatch):
    monkeypatch.setenv("VIGIL_MIN_FREE_DISK_GB", "100000")  # 100 TB floor — nothing has this
    with pytest.raises(SystemExit, match="refusing to start"):
        _check_free_disk()
