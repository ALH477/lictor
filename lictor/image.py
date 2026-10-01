"""The live, self-modifying image: the `!` namespace and `redefine()`.

`Image` is the seam `self.*` tools and the REPL's `!` forms both go
through. It owns no mutation mechanics itself -- that's `overlay.py` --
only the namespace a `!` form evaluates in, and the thin orchestration that
turns a `redefine()` call into an overlay-journal entry plus a `mutation`
record.
"""

from __future__ import annotations

import inspect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import overlay, repl


@dataclass
class Image:
    cfg: Any
    state: Any
    records: Any
    paths: Any
    registry: Any

    #: the seq most recently redefined or exercised in this process, so
    #: `self.commit`/`self.discard` can act on "the current one" without
    #: the operator having to name a target or seq again.
    last_seq: int | None = field(default=None, init=False)

    # -- the `!` namespace --------------------------------------------------

    @property
    def ns(self) -> dict[str, Any]:
        import lictor as lictor_pkg

        def _prompt(text: str, to: str | None = None) -> repl.Submission:
            return repl.Submission(text, to=to, source="image")

        def _read_file(path: str) -> str:
            return Path(path).read_text(encoding="utf-8")

        return {
            "prompt": _prompt,
            "read_file": _read_file,
            "state": self.state,
            "cfg": self.cfg,
            "registry": self.registry,
            "image": self,
            "lictor": lictor_pkg,
            "apropos": self.apropos,
        }

    def eval(self, src: str) -> Any:
        """ast-parse `src`, exec everything but a trailing bare expression,
        eval that expression and return it. This is exactly
        `repl.eval_image` -- reused rather than duplicated, since its
        signature (source, namespace) -> Any already matches."""
        return repl.eval_image(src, self.ns)

    # -- redefine ------------------------------------------------------------

    def redefine(self, target: str, source: str) -> overlay.OverlayEntry:
        """Propose and live-apply a mutation. See `overlay.redefine` for
        the full contract (shadows, schema refusal, the overlay file /
        manifest written, the `mutation` record appended)."""
        entry = overlay.redefine(
            self.paths,
            self.cfg,
            getattr(self.state, "cid", None),
            self.registry,
            target,
            source,
            records=self.records,
        )
        self.last_seq = entry.seq
        return entry

    # -- apropos --------------------------------------------------------------

    def apropos(self, pattern: str) -> list[dict[str, str]]:
        """Regex `pattern` over registry tool names and `lictor.*` module
        attributes. Returns `{"name": ..., "summary": ...}` rows: the
        tool's description (first line) for a tool, or the attribute's
        docstring (first line) for a module attribute."""
        rx = re.compile(pattern)
        results: list[dict[str, str]] = []

        for dotted, sdk_tool in self.registry.tools.items():
            if rx.search(dotted):
                desc = getattr(sdk_tool, "description", "") or ""
                results.append({"name": dotted, "summary": desc.splitlines()[0] if desc else ""})

        for mod_name in sorted(sys.modules):
            if mod_name != "lictor" and not mod_name.startswith("lictor."):
                continue
            if mod_name.startswith("lictor._overlay"):
                continue
            module = sys.modules.get(mod_name)
            if module is None:
                continue
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue
                full_name = f"{mod_name}.{attr_name}"
                if not rx.search(full_name):
                    continue
                obj = getattr(module, attr_name, None)
                doc = inspect.getdoc(obj) or ""
                results.append({"name": full_name, "summary": doc.splitlines()[0] if doc else ""})

        return results
