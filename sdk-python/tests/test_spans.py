import vigil


def test_identity_stamped_on_every_span_and_run_id_defaults_to_trace_id(spans):
    with vigil.agent_run(
        run_kind="eval", eval_run_id="batch-1", eval_case_id="case-7", session_id="sess-9"
    ):
        with vigil.tool_call(tool_name="search"):
            pass
        with vigil.llm_call(request_model="claude-haiku-4-5"):
            pass

    finished = spans()
    assert len(finished) == 3

    trace_ids = {s.context.trace_id for s in finished}
    assert len(trace_ids) == 1
    tid_hex = format(next(iter(trace_ids)), "032x")

    for s in finished:
        a = s.attributes
        assert a["vigil.run.id"] == tid_hex  # defaulted to trace id
        assert a["vigil.run.kind"] == "eval"
        assert a["vigil.eval.run_id"] == "batch-1"
        assert a["vigil.eval.case_id"] == "case-7"
        assert a["gen_ai.conversation.id"] == "sess-9"


def test_explicit_run_id_is_used(spans):
    with vigil.agent_run(run_kind="live", run_id="run-abc"):
        pass
    finished = spans()
    assert finished[0].attributes["vigil.run.id"] == "run-abc"
    assert finished[0].attributes["vigil.run.kind"] == "live"


def test_llm_call_sets_gen_ai_attributes(spans):
    with vigil.agent_run(run_kind="eval"):
        with vigil.llm_call(
            request_model="claude-haiku-4-5", params={"max_tokens": 256, "temperature": 0.5}
        ) as span:
            vigil.set_gen_ai_response(
                span, response_model="claude-haiku-4-5", input_tokens=10, output_tokens=20
            )

    llm = [s for s in spans() if s.name == "gen_ai.chat"][0]
    a = llm.attributes
    assert a["gen_ai.system"] == "anthropic"
    assert a["gen_ai.request.model"] == "claude-haiku-4-5"
    assert a["gen_ai.request.max_tokens"] == 256
    assert a["gen_ai.usage.input_tokens"] == 10
    assert a["gen_ai.usage.output_tokens"] == 20


def test_tool_call_captures_arguments_in_eval(spans):
    with vigil.agent_run(run_kind="eval"):
        with vigil.tool_call(tool_name="search", arguments={"q": "hello"}):
            pass
    tool = [s for s in spans() if s.attributes.get("gen_ai.tool.name") == "search"][0]
    assert "gen_ai.tool.arguments" in {e.name for e in tool.events}


def test_decorator_form(spans):
    @vigil.traced_agent_run(run_kind="eval", eval_run_id="b2")
    def do_work():
        return 42

    assert do_work() == 42
    finished = spans()
    assert any(s.attributes.get("vigil.eval.run_id") == "b2" for s in finished)
