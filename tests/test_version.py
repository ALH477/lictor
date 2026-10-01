"""lictor.__version__ must match the version pyproject.toml resolves to."""

import re
import tomllib
from pathlib import Path

import lictor


def test_version_matches_pyproject() -> None:
    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())

    assert pyproject["project"]["dynamic"] == ["version"]
    version_path = root / pyproject["tool"]["hatch"]["version"]["path"]

    match = re.search(r'^__version__\s*=\s*"([^"]+)"', version_path.read_text(), re.MULTILINE)
    assert match is not None, f"no __version__ assignment found in {version_path}"

    assert lictor.__version__ == match.group(1)
