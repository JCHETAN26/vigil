"""Verify that a short-lived process flushes all buffered spans on exit (the atexit
flush/shutdown registered by init), so nothing is lost when a script ends without an
explicit shutdown call."""

import subprocess
import sys
import textwrap

CHILD = textwrap.dedent(
    """
    import sys
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    import vigil

    out_path = sys.argv[1]

    class FileExporter(SpanExporter):
        def export(self, spans):
            with open(out_path, "a") as f:
                for s in spans:
                    f.write(s.name + "\\n")
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    # BatchSpanProcessor buffers; we deliberately never call shutdown() — the atexit
    # handler registered by init() must flush these before the process exits.
    vigil.init(config=vigil.Config(service_name="short-lived"), exporter=FileExporter(), set_global=True)

    with vigil.agent_run(run_kind="eval", eval_run_id="b1"):
        for i in range(5):
            with vigil.tool_call(tool_name="t%d" % i):
                pass
    # No explicit flush/shutdown; process falls off the end here.
    """
)


def test_short_lived_script_flushes_all_spans_on_exit(tmp_path):
    out = tmp_path / "spans.txt"
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"child failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"

    names = [line for line in out.read_text().splitlines() if line]
    # 1 agent.run + 5 tool spans must all have arrived.
    assert len(names) == 6, f"expected 6 spans, got {len(names)}: {names}"
    assert "agent.run" in names
    assert sum(1 for n in names if n.startswith("tool.")) == 5
