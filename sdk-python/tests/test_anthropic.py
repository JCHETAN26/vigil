import asyncio

import vigil


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class ToolBlock:
    type = "tool_use"

    def __init__(self, name, inp, id):
        self.name = name
        self.input = inp
        self.id = id


class Response:
    def __init__(self, model, usage, content, stop_reason="end_turn"):
        self.model = model
        self.usage = usage
        self.content = content
        self.stop_reason = stop_reason


def _text_and_tool_response():
    return Response(
        "claude-haiku-4-5",
        Usage(11, 22),
        [TextBlock("hi"), ToolBlock("search", {"q": "x"}, "tu_1")],
    )


# --- sync ---


class SyncMessages:
    def create(self, **kwargs):
        if kwargs.get("stream"):
            return iter(
                [
                    Obj(
                        type="message_start",
                        message=Obj(model="claude-haiku-4-5", usage=Obj(input_tokens=7)),
                    ),
                    Obj(
                        type="content_block_start",
                        content_block=Obj(
                            type="tool_use", name="search", input={"q": "x"}, id="tu_9"
                        ),
                    ),
                    Obj(type="message_delta", usage=Obj(output_tokens=13)),
                    Obj(type="message_stop"),
                ]
            )
        return _text_and_tool_response()


class SyncClient:
    def __init__(self):
        self.messages = SyncMessages()


def test_wrap_sync_records_model_tokens_and_tool_use(spans):
    client = vigil.wrap(SyncClient())
    with vigil.agent_run(run_kind="eval"):
        r = client.messages.create(
            model="claude-haiku-4-5", max_tokens=100, messages=[{"role": "user", "content": "hey"}]
        )
    assert r.model == "claude-haiku-4-5"

    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.request.model"] == "claude-haiku-4-5"
    assert s.attributes["gen_ai.request.max_tokens"] == 100
    assert s.attributes["gen_ai.usage.input_tokens"] == 11
    assert s.attributes["gen_ai.usage.output_tokens"] == 22
    # identity from the surrounding run is stamped on the gen_ai span too
    assert s.attributes["vigil.run.kind"] == "eval"
    names = {e.name for e in s.events}
    assert "gen_ai.choice" in names
    assert "gen_ai.tool_use" in names


def test_wrap_sync_streaming(spans):
    client = vigil.wrap(SyncClient())
    with vigil.agent_run(run_kind="eval"):
        events = list(
            client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=10,
                stream=True,
                messages=[{"role": "user", "content": "x"}],
            )
        )
    assert len(events) == 4
    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.usage.input_tokens"] == 7
    assert s.attributes["gen_ai.usage.output_tokens"] == 13
    assert "gen_ai.tool_use" in {e.name for e in s.events}


# --- async ---


class AsyncMessages:
    async def create(self, **kwargs):
        return Response("claude-haiku-4-5", Usage(5, 6), [TextBlock("yo")])


class AsyncClient:
    def __init__(self):
        self.messages = AsyncMessages()


def test_wrap_async_records(spans):
    client = vigil.wrap(AsyncClient())

    async def go():
        with vigil.agent_run(run_kind="eval"):
            return await client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "hey"}],
            )

    r = asyncio.run(go())
    assert r.model == "claude-haiku-4-5"
    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.usage.input_tokens"] == 5
    assert s.attributes["gen_ai.usage.output_tokens"] == 6
