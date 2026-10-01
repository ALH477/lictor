"""records.py: seq monotonic, lines parse, input precedes prompt, and the
>2000-char artifact spill convention."""

from __future__ import annotations

import json

from lictor.records import Records, list_conversations


def test_seq_is_monotonic(paths) -> None:
    records = Records(paths, "cid-1")
    assert records.append("input", {"text": "a"}) == 1
    assert records.append("prompt", {"text": "a"}) == 2
    assert records.append("result", {}) == 3


def test_lines_round_trip_as_json(paths) -> None:
    records = Records(paths, "cid-2")
    records.append("meta", {"lictor": "0.1.0"})
    records.append("input", {"text": "hello"})

    lines = list(records.iter_lines("cid-2"))
    assert [line["kind"] for line in lines] == ["meta", "input"]
    assert lines[0]["data"] == {"lictor": "0.1.0"}
    assert lines[1]["data"] == {"text": "hello"}
    assert lines[0]["seq"] == 1
    assert lines[1]["seq"] == 2
    assert lines[0]["cid"] == "cid-2"
    assert "t" in lines[0]


def test_input_precedes_prompt(paths) -> None:
    records = Records(paths, "cid-3")
    records.append("input", {"text": "do the thing"})
    records.append("prompt", {"text": "do the thing", "to": None, "source": "prose"})
    records.append("assistant_text", {"text": "ok"})
    records.append("result", {"is_error": False})

    kinds = [line["kind"] for line in records.iter_lines("cid-3")]
    assert kinds == ["input", "prompt", "assistant_text", "result"]
    assert kinds.index("input") < kinds.index("prompt")


def test_artifact_spill_over_2000_chars(paths) -> None:
    records = Records(paths, "cid-4")
    big_text = "x" * 5000

    seq = records.next_seq
    path = records.artifact_path(seq, "tool_result")
    path.write_text(big_text, encoding="utf-8")
    records.append(
        "tool_result",
        {"tool_use_id": "t1", "bytes": len(big_text), "summary": big_text[:2000], "artifact": str(path)},
    )

    assert path.exists()
    assert path.read_text(encoding="utf-8") == big_text
    assert path.parent == paths.state / "artifacts" / "cid-4"

    (line,) = list(records.iter_lines("cid-4"))
    assert len(line["data"]["summary"]) == 2000
    assert line["data"]["bytes"] == 5000
    assert line["data"]["artifact"] == str(path)


def test_artifact_path_sanitizes_tool_name(paths) -> None:
    records = Records(paths, "cid-5")
    path = records.artifact_path(7, "mcp__exs__spec check")
    assert path.name == "7-mcp__exs__spec_check.log"


def test_resuming_a_conversation_continues_the_sequence(paths) -> None:
    first = Records(paths, "cid-6")
    first.append("meta", {})
    first.append("input", {"text": "one"})

    second = Records(paths, "cid-6")  # simulates a fresh process on --resume
    seq = second.append("input", {"text": "two"})
    assert seq == 3

    kinds = [line["kind"] for line in second.iter_lines("cid-6")]
    assert kinds == ["meta", "input", "input"]


def test_list_conversations_summarizes_each_file(paths) -> None:
    records = Records(paths, "cid-7")
    records.append("meta", {"cwd": "/workspace"})
    records.append("input", {"text": "hi"})
    records.append("result", {"is_error": False})

    rows = list_conversations(paths)
    (row,) = [r for r in rows if r["cid"] == "cid-7"]
    assert row["count"] == 3
    assert row["last_kind"] == "result"
    assert row["workspace"] == "/workspace"
    assert row["first_t"] is not None


def test_jsonl_lines_are_well_formed_json(paths) -> None:
    records = Records(paths, "cid-8")
    records.append("input", {"text": "hello"})
    raw = (paths.state / "conversations" / "cid-8.jsonl").read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 1
    json.loads(lines[0])  # does not raise
