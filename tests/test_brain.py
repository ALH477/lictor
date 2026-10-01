"""brain.py against FakeTransport: submit() yields a TurnSummary, records
land in order, and state.claude_session is picked up from the init
SystemMessage."""

from __future__ import annotations

import io

from lictor.brain import Brain, TurnSummary
from lictor.config import Config
from lictor.options import State
from lictor.permissions import Approvals
from lictor.records import Records
from lictor.render import Renderer
from lictor.repl import Submission


def _build_brain(paths, transport_factory, cid="cid-brain"):
    cfg = Config.load(paths)
    state = State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid=cid)
    records = Records(paths, cid)
    approvals = Approvals()
    renderer = Renderer(out=io.StringIO())
    brain = Brain(cfg, state, records, approvals, renderer, transport_factory=transport_factory)
    return brain, state, records


async def test_submit_yields_a_turn_summary(paths, transport_factory) -> None:
    brain, _state, _records = _build_brain(paths, transport_factory)

    await brain.start(resume=None)
    summary = await brain.submit(Submission("ping"))

    assert isinstance(summary, TurnSummary)
    assert summary.is_error is False
    assert summary.subtype == "success"
    assert summary.num_turns == 1
    assert summary.session_id == "fake-session-0001"

    await brain.close()


async def test_submit_records_prompt_then_assistant_text_then_result(paths, transport_factory) -> None:
    brain, _state, records = _build_brain(paths, transport_factory, cid="cid-brain-2")

    await brain.start(resume=None)
    await brain.submit(Submission("ping"))
    await brain.close()

    kinds = [line["kind"] for line in records.iter_lines("cid-brain-2")]
    assert "prompt" in kinds
    assert "assistant_text" in kinds
    assert "result" in kinds
    assert kinds.index("prompt") < kinds.index("assistant_text") < kinds.index("result")


async def test_state_claude_session_set_from_init(paths, transport_factory) -> None:
    brain, state, _records = _build_brain(paths, transport_factory, cid="cid-brain-3")

    assert state.claude_session is None
    await brain.start(resume=None)
    await brain.submit(Submission("ping"))

    assert state.claude_session == "fake-session-0001"
    assert state.model == "claude-fake-1"

    await brain.close()


async def test_prompt_record_contains_submission_text(paths, transport_factory) -> None:
    brain, _state, records = _build_brain(paths, transport_factory, cid="cid-brain-4")

    await brain.start(resume=None)
    await brain.submit(Submission("hello world", to=None, source="prose"))
    await brain.close()

    prompt_lines = [line for line in records.iter_lines("cid-brain-4") if line["kind"] == "prompt"]
    assert prompt_lines[0]["data"]["text"] == "hello world"


async def test_interrupt_appends_interrupt_record(paths, transport_factory) -> None:
    brain, _state, records = _build_brain(paths, transport_factory, cid="cid-brain-5")

    await brain.start(resume=None)
    await brain.interrupt()
    await brain.close()

    kinds = [line["kind"] for line in records.iter_lines("cid-brain-5")]
    assert "interrupt" in kinds
