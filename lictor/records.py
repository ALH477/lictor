"""Append-only JSONL conversation records.

One file per conversation at ``<state>/conversations/<cid>.jsonl``, one JSON
object per line, each written with ``flush()`` + ``os.fsync()`` before
``append()`` returns -- so a crash mid-session loses at most the record that
was in flight, never one already appended.

Kinds used in wave 1: ``meta, input, prompt, assistant_text, tool_use,
tool_result, result, command, image, thinking, interrupt, error``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Records:
    paths: Any  # a config.Paths (kept as Any to avoid an import cycle)
    cid: str

    def __post_init__(self) -> None:
        conv_dir = self.paths.state / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        self._path = conv_dir / f"{self.cid}.jsonl"
        self._seq = 0
        # A resumed conversation re-opens the same file: pick up the
        # sequence where the last process left off rather than restarting
        # at 1 and colliding with existing lines.
        if self._path.exists():
            for obj in self.iter_lines(self.cid):
                seq = obj.get("seq")
                if isinstance(seq, int) and seq > self._seq:
                    self._seq = seq

    @property
    def next_seq(self) -> int:
        """The seq the next append() call will assign. Read-only; lets a
        caller that wants to name an artifact file after the record it is
        about to write (e.g. a spilled tool result) predict it ahead of
        time, without a reserve/commit dance."""
        return self._seq + 1

    def append(self, kind: str, data: dict[str, Any]) -> int:
        self._seq += 1
        record = {
            "seq": self._seq,
            "t": _now_iso(),
            "cid": self.cid,
            "kind": kind,
            "data": data,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return self._seq

    def iter_lines(self, cid: str | None = None) -> Iterator[dict[str, Any]]:
        path = self._path if cid in (None, self.cid) else self.paths.state / "conversations" / f"{cid}.jsonl"
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue

    def artifact_path(self, seq: int, tool: str) -> Path:
        safe_tool = _SAFE_CHARS.sub("_", tool) or "tool"
        d = self.paths.state / "artifacts" / self.cid
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{seq}-{safe_tool}.log"


def list_conversations(paths: Any) -> list[dict[str, Any]]:
    """One summary row per ``<cid>.jsonl`` under ``<state>/conversations/``."""
    conv_dir = paths.state / "conversations"
    rows: list[dict[str, Any]] = []
    if not conv_dir.is_dir():
        return rows
    for file_path in sorted(conv_dir.glob("*.jsonl")):
        cid = file_path.stem
        first_t: str | None = None
        last_kind: str | None = None
        count = 0
        workspace: str | None = None
        with file_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                count += 1
                if first_t is None:
                    first_t = obj.get("t")
                last_kind = obj.get("kind")
                if obj.get("kind") == "meta":
                    workspace = obj.get("data", {}).get("cwd")
        rows.append(
            {"cid": cid, "first_t": first_t, "last_kind": last_kind, "count": count, "workspace": workspace}
        )
    return rows
