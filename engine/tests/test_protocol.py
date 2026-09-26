"""Unit tests for the parent<->worker line protocol (design §3.2): round-trip encoding, and
the dedicated results fd carrying only result records (immune to stray stdout)."""

from __future__ import annotations

import os

from engine.runner import protocol


def test_encode_decode_roundtrip():
    msg = protocol.unit_message(
        agent_version="v1",
        case_id="c0",
        trial=2,
        input={"question": "2+2"},
        expected={"answer": "4"},
    )
    line = protocol.encode(msg)
    assert line.endswith(b"\n")
    assert protocol.decode(line) == msg
    assert protocol.decode(line.strip()) == msg  # newline-agnostic decode


def test_drain_message():
    assert protocol.decode(protocol.encode(protocol.drain_message())) == {"type": "drain"}


def test_iter_lines_skips_blanks():
    blob = (
        protocol.encode({"type": "result", "a": 1}).decode()
        + "\n  \n"
        + protocol.encode({"type": "result", "a": 2}).decode()
    )
    msgs = list(protocol.iter_lines(blob))
    assert [m["a"] for m in msgs] == [1, 2]


def test_dedicated_fd_carries_only_results():
    # The worker writes result records to a dedicated fd, not stdout: reading that fd yields
    # exactly the records written to it, regardless of anything printed elsewhere.
    r_fd, w_fd = os.pipe()
    with os.fdopen(w_fd, "wb", buffering=0) as w:
        w.write(protocol.encode({"type": "result", "case_id": "c0", "trial": 0}))
        w.write(protocol.encode({"type": "result", "case_id": "c0", "trial": 1}))
    with os.fdopen(r_fd, "rb") as r:
        records = [protocol.decode(line) for line in r]
    assert [rec["trial"] for rec in records] == [0, 1]
    assert all(rec["type"] == "result" for rec in records)
