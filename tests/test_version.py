"""The package version is the one declared in ``pyproject.toml``."""

import tomllib
from pathlib import Path

import ghostwriter


def test_version_matches_pyproject() -> None:
    """``ghostwriter.__version__`` equals ``[project].version``, 0.5.0 for this release."""
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert ghostwriter.__version__ == data["project"]["version"] == "0.5.0"
