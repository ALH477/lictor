from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from lictor import config as config_mod
from lictor import rlm


def _make_ctx(paths, *, total_turns=40, cid="rlm-test-cid"):
    cfg = config_mod.Config()
    cfg.rlm.model = "haiku"
    cfg.rlm.concurrency = 2
    state = SimpleNamespace(workspace=paths.state, cid=cid)
    return SimpleNamespace(cfg=cfg, state=state, paths=paths, budget=rlm.Budget(total_turns=total_turns))


def _result_message(**overrides):
    base = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "fake-sub-session",
        "total_cost_usd": 0.0005,
        "usage": {"input_tokens": 3, "output_tokens": 5},
        "result": "the answer",
        "structured_output": None,
        "terminal_reason": "completed",
    }
    base.update(overrides)
    return ResultMessage(**base)


# -- Budget -----------------------------------------------------------------


def test_budget_take_exhausts_and_raises_with_what_was_left():
    budget = rlm.Budget(total_turns=3)
    budget.take(2)
    assert budget.used == 2
    assert budget.remaining() == 1

    with pytest.raises(rlm.BudgetExhausted) as excinfo:
        budget.take(2)
    assert excinfo.value.requested == 2
    assert excinfo.value.left == 1
    # a failed take must not partially spend
    assert budget.used == 2


# -- infer() against a fake query() -----------------------------------------


async def test_infer_writes_trace_with_context_hashes_and_budget(paths, monkeypatch, tmp_path):
    captured = {}

    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        yield AssistantMessage(content=[TextBlock(text="thinking")], model="fake")
        yield _result_message(result="42", structured_output={"answer": 42}, session_id="sub-1")

    monkeypatch.setattr(rlm, "query", fake_query)

    ctx = _make_ctx(paths, total_turns=10)

    ctx_file = tmp_path / "notes.txt"
    ctx_file.write_text("some context content", encoding="utf-8")
    expected_sha = hashlib.sha256(ctx_file.read_bytes()).hexdigest()

    trace = await rlm.infer(
        ctx,
        "what is the answer?",
        context=[str(ctx_file)],
        schema={"type": "object", "properties": {"answer": {"type": "integer"}}},
        max_turns=2,
    )

    assert trace.result == "42"
    assert trace.structured_output == {"answer": 42}
    assert trace.session_id == "sub-1"
    assert trace.parent_cid == "rlm-test-cid"
    assert trace.budget == {"before": 0, "after": 2}
    assert ctx.budget.used == 2
    assert trace.error is None
    assert trace.id.startswith("inf-")

    assert len(trace.context) == 1
    assert trace.context[0]["path"] == str(ctx_file)
    assert trace.context[0]["sha256"] == expected_sha
    assert trace.context[0]["bytes"] == len("some context content")

    # the context must actually have been inlined into the prompt sent on
    assert expected_sha in captured["prompt"]
    assert "some context content" in captured["prompt"]
    # no tools, no memory, dontAsk, no setting sources, per the contract
    opts = captured["options"]
    assert opts.tools == []
    assert opts.allowed_tools == []
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.max_turns == 2
    assert opts.output_format == {"type": "json_schema", "schema": trace.schema}

    # the Trace file on disk matches the plan's shape (plus `error`)
    trace_path = paths.data / "inferences" / f"{trace.id}.json"
    on_disk = json.loads(trace_path.read_text(encoding="utf-8"))
    expected_keys = {
        "id", "parent_cid", "t", "model", "effort", "prompt", "context",
        "schema", "result", "structured_output", "usage", "session_id",
        "ms", "budget", "error",
    }
    assert set(on_disk.keys()) == expected_keys
    assert on_disk["result"] == "42"
    assert on_disk["budget"] == {"before": 0, "after": 2}


async def test_infer_records_a_failed_call_without_raising(paths, monkeypatch):
    async def exploding_query(*, prompt, options):
        raise RuntimeError("the sub-model fell over")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(rlm, "query", exploding_query)
    ctx = _make_ctx(paths)

    trace = await rlm.infer(ctx, "will this fail?")
    assert trace.result is None
    assert trace.error == {"type": "RuntimeError", "message": "the sub-model fell over"}
    # budget was still spent -- the attempt happened
    assert trace.budget == {"before": 0, "after": 1}


async def test_infer_refuses_oversized_context_without_spending_budget(paths, monkeypatch, tmp_path):
    async def fake_query(*, prompt, options):
        yield _result_message()

    monkeypatch.setattr(rlm, "query", fake_query)
    ctx = _make_ctx(paths)

    huge = tmp_path / "huge.txt"
    huge.write_bytes(b"x" * (rlm.MAX_CONTEXT_FILE_BYTES + 1))

    with pytest.raises(rlm.ContextTooLarge):
        await rlm.infer(ctx, "describe this", context=[str(huge)])

    assert ctx.budget.used == 0
    assert list((paths.data / "inferences").glob("*.json")) == []


async def test_infer_propagates_budget_exhausted(paths, monkeypatch):
    async def fake_query(*, prompt, options):
        yield _result_message()

    monkeypatch.setattr(rlm, "query", fake_query)
    ctx = _make_ctx(paths, total_turns=1)

    with pytest.raises(rlm.BudgetExhausted):
        await rlm.infer(ctx, "too expensive", max_turns=2)


# -- map_() -------------------------------------------------------------


async def test_map_keeps_one_failure_from_killing_the_batch(paths, monkeypatch):
    async def flaky_query(*, prompt, options):
        if "FAIL" in prompt:
            raise RuntimeError("boom")
        yield _result_message(result="ok")

    monkeypatch.setattr(rlm, "query", flaky_query)
    ctx = _make_ctx(paths, total_turns=10)

    traces = await rlm.map_(ctx, "describe", ["good-1", "FAIL-this-one", "good-2"], concurrency=2)

    assert len(traces) == 3
    results = [t.result for t in traces]
    errors = [t.error for t in traces]
    assert results.count("ok") == 2
    assert sum(1 for e in errors if e is not None) == 1
    failed = next(t for t in traces if t.error is not None)
    assert failed.error["type"] == "RuntimeError"
