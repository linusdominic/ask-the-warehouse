"""The conversation loop and the model client, with a scripted model and no database."""

import json
from types import SimpleNamespace

import pytest

from askwh.agent import run_turn, system_prompt, tools_for
from askwh.llm import LLM, BudgetExceeded, price
from askwh.tools import Outcome
from tests.conftest import NOW, FakeLLM, text, tool, turn


class StubToolbox:
    """Answers tool calls from a table, so the loop can be tested without a warehouse."""

    def __init__(self, catalog, outcomes: dict[str, Outcome]) -> None:
        self.catalog = catalog
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, tool_input):
        self.calls.append((name, tool_input))
        return self.outcomes[name]


async def run(llm, toolbox, message="What are sales today?", history=None, **kwargs):
    return await run_turn(
        llm, toolbox, mode="semantic", model="m", system="s", history=history or [],
        user_message=message, **kwargs,
    )  # fmt: skip


RESULT = Outcome(
    '{"rows": [{"value": 1515.6}]}', event={"type": "result", "rows": [{"value": 1515.6}]}
)


async def test_tool_call_then_answer(catalog):
    llm = FakeLLM([
        turn(text("Let me check."), tool("query_metric", metric="net_sales")),
        turn(text("Net sales today are $1,516.")),
    ])  # fmt: skip
    box = StubToolbox(catalog, {"query_metric": RESULT})
    seen = []

    async def emit(event):
        seen.append(event["type"])

    result = await run(llm, box, emit=emit)

    assert (result.text, result.stopped, result.steps) == (
        "Net sales today are $1,516.",
        "answer",
        2,
    )
    assert box.calls == [("query_metric", {"metric": "net_sales"})]
    assert [e["type"] for e in result.events] == ["tool_call", "result"]
    # Words said before a tool call are streamed, then withdrawn: they are not the answer.
    assert seen == ["text", "text_reset", "tool_call", "result", "text"]
    assert (result.input_tokens, result.cost_usd) == (200, 0.002)
    # The second request carries the tool result back to the model.
    sent = llm.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": RESULT.content}
    ]


async def test_tool_error_goes_back_to_the_model_marked_as_an_error(catalog):
    llm = FakeLLM(
        [turn(tool("query_metric", metric="profit")), turn(text("I cannot answer that."))]
    )
    box = StubToolbox(catalog, {"query_metric": Outcome("Unknown metric 'profit'.", is_error=True)})

    result = await run(llm, box)

    assert llm.requests[1]["messages"][2]["content"][0]["is_error"] is True
    assert {
        "type": "tool_error",
        "name": "query_metric",
        "message": "Unknown metric 'profit'.",
    } in (result.events)


async def test_clarification_ends_the_turn_and_the_reply_joins_the_pending_result(catalog):
    clarify = Outcome(
        "The question was shown to the user.",
        event={"type": "clarify", "question": "Sales or production?", "options": ["Sales"]},
        ends_turn=True,
    )
    llm = FakeLLM([turn(tool("ask_clarification", question="Sales or production?"))])
    box = StubToolbox(catalog, {"ask_clarification": clarify, "query_metric": RESULT})

    first = await run(llm, box, message="How are we doing?")

    assert (first.text, first.stopped, first.steps) == ("Sales or production?", "clarification", 1)

    llm.turns = [turn(tool("query_metric", "call_2", metric="net_sales")), turn(text("$1,516."))]
    second = await run(llm, box, message="Sales", history=first.messages)

    assert second.stopped == "answer"
    # No two user messages in a row: the reply was added to the message holding the tool result.
    roles = [m["role"] for m in llm.requests[1]["messages"]]
    assert roles == ["user", "assistant", "user"]
    pending = llm.requests[1]["messages"][2]["content"]
    assert [block["type"] for block in pending] == ["tool_result", "text"]
    assert pending[1]["text"] == "Sales"


async def test_loop_stops_after_the_step_limit(catalog):
    llm = FakeLLM([turn(tool("query_metric", f"call_{i}", metric="net_sales")) for i in range(5)])
    box = StubToolbox(catalog, {"query_metric": RESULT})

    result = await run(llm, box, max_steps=3)

    assert (result.stopped, result.steps) == ("max_steps", 3)
    assert "allowed number of steps" in result.text


def test_each_mode_gets_its_own_instructions_and_tools(catalog):
    schema = "TABLE analytics.fact_orders"
    semantic = system_prompt("semantic", catalog, schema)
    glossary = system_prompt("sql_glossary", catalog, schema)
    bare = system_prompt("sql_schema", catalog, schema)

    assert "2026" not in semantic  # nothing that changes between turns
    assert "net_sales [usd]" in semantic and schema not in semantic
    assert (
        schema in glossary and "BUSINESS DEFINITIONS" in glossary and "net_sales [usd]" in glossary
    )
    assert schema in bare and "net_sales" not in bare  # the bare baseline never sees definitions

    assert [t["name"] for t in tools_for("semantic", catalog)] == [
        "query_metric", "list_dimension_values", "ask_clarification", "report_gap",
        "propose_digest", "propose_alert",
    ]  # fmt: skip
    assert [t["name"] for t in tools_for("sql_schema", catalog)] == ["run_sql", "ask_clarification"]
    metric_enum = tools_for("semantic", catalog)[0]["input_schema"]["properties"]["metric"]["enum"]
    assert metric_enum == list(catalog.metrics)


# --- the model client ---------------------------------------------------------------------


class FakeStream:
    def __init__(self, message, chunks) -> None:
        self._message, self._chunks = message, chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    @property
    def text_stream(self):
        async def chunks():
            for chunk in self._chunks:
                yield chunk

        return chunks()

    async def get_final_message(self):
        return self._message


class FakeMessages:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def stream(self, **request):
        self.requests.append(request)
        blocks = [
            SimpleNamespace(model_dump=lambda **_: {"type": "thinking", "thinking": "", "signature": "x"}),
            SimpleNamespace(model_dump=lambda **_: {"type": "text", "text": "Hello there", "citations": None}),
            SimpleNamespace(
                model_dump=lambda **_: {
                    "type": "tool_use", "id": "t1", "name": "query_metric",
                    "input": {"metric": "net_sales"}, "caller": {"type": "direct"},
                }
            ),
        ]  # fmt: skip
        message = SimpleNamespace(
            content=blocks,
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )
        return FakeStream(message, ["Hello ", "there"])


def make_llm(tmp_path, **kwargs):
    messages = FakeMessages()
    client = SimpleNamespace(messages=messages)
    return LLM(client=client, ledger_path=tmp_path / "spend.json", **kwargs), messages


def ask(llm, prompt="hi", **kwargs):
    return llm.respond(
        model="claude-haiku-5-5",
        system="s",
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        **kwargs,
    )


async def test_turn_is_streamed_priced_and_normalised(tmp_path):
    llm, messages = make_llm(tmp_path)
    heard = []

    async def on_text(chunk):
        heard.append(chunk)

    reply = await ask(llm, on_text=on_text)

    assert heard == ["Hello ", "there"] and reply.text == "Hello there"
    assert reply.tool_calls == [
        {"type": "tool_use", "id": "t1", "name": "query_metric", "input": {"metric": "net_sales"}}
    ]
    # Thinking blocks are kept exactly; other blocks are trimmed to what the API accepts back.
    assert reply.content[0] == {"type": "thinking", "thinking": "", "signature": "x"}
    assert reply.content[1] == {"type": "text", "text": "Hello there"}
    assert reply.cost_usd == pytest.approx(price("claude-haiku-5-5", 1000, 200))
    assert json.loads((tmp_path / "spend.json").read_text())["calls"] == 1
    assert messages.requests[0]["output_config"] == {"effort": "low"}


async def test_cache_replays_a_turn_without_calling_or_charging(tmp_path):
    llm, messages = make_llm(tmp_path, cache_dir=tmp_path / "cache")
    first = await ask(llm)
    heard = []

    async def on_text(chunk):
        heard.append(chunk)

    again = await ask(llm, on_text=on_text)

    assert (first.cached, again.cached) == (False, True)
    assert again.content == first.content and heard == ["Hello there"]
    assert len(messages.requests) == 1 and llm.ledger.data["calls"] == 1


async def test_no_cache_unless_asked_for(tmp_path):
    llm, messages = make_llm(tmp_path)
    await ask(llm)
    await ask(llm)
    assert len(messages.requests) == 2


async def test_budget_refuses_further_calls(tmp_path):
    llm, messages = make_llm(tmp_path, budget_usd=0.00015)
    await ask(llm, "one")  # $0.0002
    with pytest.raises(BudgetExceeded):
        await ask(llm, "two")
    assert len(messages.requests) == 1


async def test_time_travels_with_the_user_message_and_reasoning_is_not_kept(catalog):
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
    llm = FakeLLM([
        turn(thinking, tool("query_metric", metric="net_sales")),
        turn(thinking, text("Net sales today are $1,516.")),
    ])  # fmt: skip
    box = StubToolbox(catalog, {"query_metric": RESULT})

    result = await run(llm, box, now=NOW)

    first_message = llm.requests[0]["messages"][0]["content"]
    assert first_message.endswith("[Current business time: Tuesday 17 November 2026, 12:00 CST]")
    # Inside the turn the reasoning block went back to the model untouched...
    assert llm.requests[1]["messages"][1]["content"][0] == thinking
    # ...but the history saved for the next turn has none, so a changed prompt cannot break it.
    saved_types = [
        block["type"]
        for m in result.messages
        if isinstance(m["content"], list)
        for block in m["content"]
    ]
    assert "thinking" not in saved_types and saved_types == ["tool_use", "tool_result", "text"]
