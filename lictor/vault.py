"""The crash-surviving input vault.

A submission's lifecycle is: queued -> inflight -> gone (done). Every
write is a full rewrite of ``<state>/vault/<cid>.json`` done atomically
(write to a sibling ``.tmp`` file, ``fsync``, ``os.replace``), so a
``kill -9`` can only ever land between two consistent states of the
file -- never mid-write. In particular, a kill between ``mark_inflight``
and ``done`` leaves the item on disk as ``inflight``, which is the whole
point: the next process to open this vault sees it and can offer to
resubmit it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Statuses an item can be in. There is no "done" status -- done means
#: removed from the file.
STATUSES = ("queued", "inflight")


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Vault:
    paths: Any  # a config.Paths
    cid: str

    def __post_init__(self) -> None:
        vault_dir = self.paths.state / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)
        self._path = vault_dir / f"{self.cid}.json"
        if not self._path.exists():
            self._write({"cid": self.cid, "items": []})

    # -- atomic persistence -------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"cid": self.cid, "items": []}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {"cid": self.cid, "items": []}
        doc.setdefault("cid", self.cid)
        doc.setdefault("items", [])
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        tmp = self._path.with_name(self._path.name + f".tmp-{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    # -- lifecycle ------------------------------------------------------

    def add(self, text: str, to: str | None = None) -> str:
        """Queue `text`, returning its item id."""
        doc = self._read()
        item_id = uuid.uuid4().hex[:12]
        doc["items"].append(
            {
                "id": item_id,
                "t": _now_iso(),
                "text": text,
                "to": to,
                "status": "queued",
                "attempts": 0,
            }
        )
        self._write(doc)
        return item_id

    def mark_inflight(self, item_id: str) -> None:
        doc = self._read()
        for item in doc["items"]:
            if item["id"] == item_id:
                item["status"] = "inflight"
                item["attempts"] = item.get("attempts", 0) + 1
                break
        else:
            raise KeyError(item_id)
        self._write(doc)

    def done(self, item_id: str) -> None:
        """Remove the item: it was sent and the turn completed."""
        doc = self._read()
        remaining = [item for item in doc["items"] if item["id"] != item_id]
        if len(remaining) == len(doc["items"]):
            raise KeyError(item_id)
        doc["items"] = remaining
        self._write(doc)

    def items(self) -> list[dict[str, Any]]:
        return self._read()["items"]

    def restore_order(self) -> list[dict[str, Any]]:
        """Queued and inflight items, in the order they were added.

        The file already holds items in insertion (append) order, and
        nothing in this module ever reorders the list, so this is just
        the items whose status is still outstanding.
        """
        return [item for item in self._read()["items"] if item["status"] in STATUSES]

    def discard_all(self) -> Path | None:
        """Archive every item to a sibling ``.discarded-<ts>.json`` file
        and leave this vault empty. Returns the archive path, or ``None``
        if there was nothing to discard."""
        doc = self._read()
        if not doc["items"]:
            return None
        ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archive_path = self._path.with_name(f"{self.cid}.discarded-{ts}.json")
        with archive_path.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._write({"cid": self.cid, "items": []})
        return archive_path

    def summary(self) -> str | None:
        """A one-line startup-banner summary, or ``None`` if empty."""
        pending = self.restore_order()
        if not pending:
            return None
        oldest = pending[0]
        snippet = oldest["text"][:60]
        plural = "" if len(pending) == 1 else "s"
        return f"{len(pending)} pending item{plural}; oldest ({oldest['status']}): {snippet!r}"
