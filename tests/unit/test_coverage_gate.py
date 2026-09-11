"""Unit tests for the Coverage_Gate configuration and script (task 8.5).

Requirement 10.6: a Coverage_Gate exits with code 1 and lists the modules below
the floor when ``spec_pipeline`` statement coverage falls below 80 percent.
Requirement 10.9: the floor lives in the version-controlled Project_Manifest.

These tests assert only that the gate is CONFIGURED and PRESENT; they do NOT run
the real coverage gate and do NOT require the 80 percent floor to be met in this
run. CI runs the real gate (``scripts/coverage_gate.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

try:  # tomllib is stdlib on 3.11+; fall back to tomli if present.
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter version
    import tomli as _toml  # type: ignore[no-redef]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_GATE_SCRIPT = _REPO_ROOT / "scripts" / "coverage_gate.py"


def test_pyproject_fail_under_is_80():
    """The Project_Manifest configures an 80 percent floor (Requirement 10.9)."""
    with open(_PYPROJECT, "rb") as fh:
        data = _toml.load(fh)
    fail_under = data["tool"]["coverage"]["report"]["fail_under"]
    assert fail_under == 80


def test_coverage_gate_source_targets_spec_pipeline():
    """Coverage is measured against ``spec_pipeline`` (Requirement 10.6)."""
    with open(_PYPROJECT, "rb") as fh:
        data = _toml.load(fh)
    source = data["tool"]["coverage"]["run"]["source"]
    assert "spec_pipeline" in source


def test_coverage_gate_script_exists_and_parses():
    """``scripts/coverage_gate.py`` is present and is valid Python (Requirement 10.6)."""
    assert _GATE_SCRIPT.exists(), "scripts/coverage_gate.py must exist"
    source = _GATE_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)  # raises SyntaxError if the script does not parse.

    # The gate exposes an 80-floor-driven entry point and lists modules below floor.
    func_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    assert {"read_floor", "modules_below_floor", "main"} <= func_names
    # The gate runs pytest under coverage for spec_pipeline with the floor enforced.
    assert "--cov=" in source
    assert "spec_pipeline" in source
    assert "--cov-fail-under" in source
