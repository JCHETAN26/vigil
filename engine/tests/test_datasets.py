"""Unit tests for the local JSON dataset adapter and the adapter registry (design §5)."""

from __future__ import annotations

import json

import pytest

from engine.datasets import LocalJSONAdapter, get_adapter
from engine.datasets.base import Case, register_adapter


def _write(tmp_path, doc):
    p = tmp_path / "suite.json"
    p.write_text(json.dumps(doc))
    return str(p)


def test_load_cases(tmp_path):
    path = _write(
        tmp_path,
        {
            "version": "2026-09-25",
            "cases": [
                {
                    "case_id": "calc-1",
                    "input": "What is 23 * 19?",
                    "expected": {"answer": "437", "tool_calls": ["calculator"]},
                    "tags": ["math"],
                },
                {
                    "case_id": "wx-1",
                    "input": {"question": "Weather in Paris?"},
                    "expected": {"tool_calls": ["get_weather"]},
                },
            ],
        },
    )
    adapter = LocalJSONAdapter(path)
    assert adapter.name == "local"
    assert adapter.version == "2026-09-25"
    cases = list(adapter.load())
    assert [c.case_id for c in cases] == ["calc-1", "wx-1"]
    assert isinstance(cases[0], Case)
    assert cases[0].tags == ["math"]
    assert cases[1].input == {"question": "Weather in Paris?"}
    assert cases[1].tags == []


def test_version_override(tmp_path):
    path = _write(tmp_path, {"version": "file-v", "cases": []})
    assert LocalJSONAdapter(path, version="override-v").version == "override-v"


def test_from_config(tmp_path):
    path = _write(tmp_path, {"version": "v", "cases": []})
    adapter = LocalJSONAdapter.from_config({"path": path, "version": "cfg-v"})
    assert adapter.version == "cfg-v"
    with pytest.raises(ValueError, match="requires a 'path'"):
        LocalJSONAdapter.from_config({})


def test_duplicate_case_id_rejected(tmp_path):
    path = _write(
        tmp_path,
        {
            "cases": [
                {"case_id": "d", "expected": {}},
                {"case_id": "d", "expected": {}},
            ]
        },
    )
    with pytest.raises(ValueError, match="duplicate case_id"):
        list(LocalJSONAdapter(path).load())


def test_missing_expected_rejected(tmp_path):
    path = _write(tmp_path, {"cases": [{"case_id": "x", "input": "hi"}]})
    with pytest.raises(ValueError, match="no 'expected'"):
        list(LocalJSONAdapter(path).load())


def test_registry():
    assert get_adapter("local") is LocalJSONAdapter
    with pytest.raises(KeyError, match="no dataset adapter"):
        get_adapter("nope")

    with pytest.raises(ValueError, match="no non-empty 'name'"):

        @register_adapter
        class _NoName:  # noqa: N801
            pass
