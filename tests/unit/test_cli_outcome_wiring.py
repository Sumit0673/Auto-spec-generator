"""AST-level checks that ``spec_pipeline/cli.py`` funnels through the table.

Task 7.1 (Requirements 20.6, 20.10, 21.2). ``cli.py`` imports the pipeline (a
slither-backed chain) at module top, so it cannot be imported for real in the
network-disabled, tool-absent test environment. We therefore parse it with the
``ast`` module and assert the structural guarantees the task requires:

* it imports the shared Outcome->exit-code table (``exit_code_for``) and the
  redaction helper (``redact_secrets``) from :mod:`spec_pipeline.outcomes`;
* it never calls the bare ``sys.exit(1)`` / ``sys.exit(0)`` shortcuts that would
  bypass the table (every terminal state must go through ``exit_code_for`` or
  the ``_finish`` helper that wraps it);
* it references the ``PipelineOutcome`` signal raised by task 4.2.

The behavior of the table itself is covered by ``test_outcomes.py``; here we only
prove the CLI is wired to it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parents[2] / "spec_pipeline" / "cli.py"
_SOURCE = _CLI_PATH.read_text()
_TREE = ast.parse(_SOURCE)


def test_cli_parses():
    """The CLI is syntactically valid Python (ast.parse succeeded above)."""
    assert isinstance(_TREE, ast.Module)


def _imported_names_from(module_suffix: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith(module_suffix):
                names.update(alias.name for alias in node.names)
    return names


def test_imports_shared_table_and_redaction():
    """CLI imports exit_code_for + redact_secrets from spec_pipeline.outcomes."""
    imported = _imported_names_from("outcomes")
    assert "exit_code_for" in imported
    assert "redact_secrets" in imported


def test_imports_pipeline_outcome_signal():
    """CLI imports the PipelineOutcome signal raised by task 4.2 (Req 20.10)."""
    imported = _imported_names_from("pipeline")
    assert "PipelineOutcome" in imported


def _sys_exit_arguments() -> list[ast.expr]:
    """Return the argument node of every ``sys.exit(...)`` call in the CLI."""
    args: list[ast.expr] = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "exit"
            and isinstance(func.value, ast.Name)
            and func.value.id == "sys"
        ):
            if node.args:
                args.append(node.args[0])
    return args


def test_no_hardcoded_sys_exit_literals():
    """No ``sys.exit(<int literal>)`` bypasses the Outcome->exit-code table.

    Every terminal state must go through ``exit_code_for`` or the ``_finish``
    helper (which itself calls ``exit_code_for``), so no bare integer literal
    (in particular ``sys.exit(1)`` for a failing verdict or ``sys.exit(0)`` for
    everything) may appear (Requirement 20.10).
    """
    for arg in _sys_exit_arguments():
        assert not isinstance(arg, ast.Constant) or not isinstance(
            arg.value, int
        ), f"cli.py has a hardcoded sys.exit({ast.dump(arg)}); use exit_code_for"


def test_every_sys_exit_routes_through_the_table():
    """Each ``sys.exit`` argument is an ``exit_code_for(...)`` or ``_finish(...)`` call."""
    allowed = {"exit_code_for", "_finish"}
    for arg in _sys_exit_arguments():
        assert isinstance(arg, ast.Call), (
            f"sys.exit argument {ast.dump(arg)} is not a call through the table"
        )
        func = arg.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        assert name in allowed, (
            f"sys.exit routes through {name!r}, expected one of {allowed}"
        )
