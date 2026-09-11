"""Unit tests for ``--typecheck-only`` pipeline wiring.

Covers the wiring added for typecheck-only mode:

* ``run_pipeline(..., typecheck_only=True)`` forces the keyless Stage 3 loop
  (``write_rules_iterative(force_keyless=True)``) and runs Stage 5 via
  ``verify_with_prover(typecheck_only=True)``, mapping the local typecheck
  result to ``results["outcome"]`` / ``results["exit_code"]`` so a clean run
  exits 0 and NEVER triggers a cloud call.
* the CLI defines ``--typecheck-only`` and threads it into both
  ``run_pipeline(...)`` and ``run_single_stage(...)`` (ast-level, since
  ``cli.py`` imports the slither-backed pipeline chain at module top and cannot
  be imported for real here).

Follows the offline stub pattern of ``test_pipeline_cache_flags.py``: slither is
uninstallable here, so slither-free stubs are registered before loading
``spec_pipeline.stage1_extract``, and ``pipeline.py`` is loaded by path with the
LLM/verifier-backed stages replaced by recording spies.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_stub_packages() -> None:
    if "solidity_graph.analyzer" not in sys.modules:
        analyzer = types.ModuleType("solidity_graph.analyzer")
        analyzer.SolidityAnalyzer = object
        analyzer.SolidityGraph = object
        analyzer.ContractInfo = object
        analyzer.FunctionNode = object
        analyzer._find_solc = lambda *a, **k: None
        analyzer._SHARED_DEPS = Path("/nonexistent-shared-deps")
        analyzer._build_solc_remaps = lambda *a, **k: []
        sys.modules["solidity_graph.analyzer"] = analyzer
    if "solidity_graph" not in sys.modules or not hasattr(
        sys.modules["solidity_graph"], "__path__"
    ):
        sg = types.ModuleType("solidity_graph")
        sg.__path__ = [str(_REPO_ROOT / "solidity_graph")]
        sg.analyzer = sys.modules["solidity_graph.analyzer"]
        sys.modules["solidity_graph"] = sg
    if "spec_pipeline" not in sys.modules or not hasattr(
        sys.modules["spec_pipeline"], "__path__"
    ):
        sp = types.ModuleType("spec_pipeline")
        sp.__path__ = [str(_REPO_ROOT / "spec_pipeline")]
        sys.modules["spec_pipeline"] = sp


def _load_module_by_path(mod_name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_install_stub_packages()

if "spec_pipeline.stage1_extract" not in sys.modules:
    _stage1 = _load_module_by_path(
        "spec_pipeline.stage1_extract", "spec_pipeline/stage1_extract.py"
    )
else:  # pragma: no cover
    _stage1 = sys.modules["spec_pipeline.stage1_extract"]

FirstPartyContract = _stage1.FirstPartyContract
Stage1Table = _stage1.Stage1Table


class _CloudCalledError(AssertionError):
    """Raised if a cloud rule-proof is ever attempted in typecheck_only mode."""


def _make_verify_spy(record: dict):
    """A verify_with_prover stand-in recording the typecheck_only kwarg.

    It NEVER performs a cloud call: it asserts ``typecheck_only=True`` was passed
    and returns a ``typecheck_passed`` report (the keyless clean result), so a
    typecheck-only pipeline run maps to exit 0 without any cloud verdict.
    """

    def verify_with_prover(source_path, cvl_spec, output_dir, certora_args=None,
                           *, table=None, run_local_typecheck=False,
                           typecheck_only=False, **kwargs):
        record["called"] = True
        record["typecheck_only"] = typecheck_only
        if not typecheck_only:
            # In this test suite the pipeline runs are always typecheck_only, so
            # a cloud-verdict path (typecheck_only False) would be a wiring bug.
            raise _CloudCalledError(
                "verify_with_prover called without typecheck_only in a "
                "typecheck_only run (cloud proof would run)"
            )
        return {
            "status": "typecheck_passed",
            "summary": {
                "total_rules": 0, "passed": 0, "vacuous": 0, "failed": 0,
                "dead": 0, "timeout": 0, "pass_rate": None,
            },
            "rules": [],
            "local_typecheck_passed": True,
        }

    return verify_with_prover


def _make_iterative_spy(record: dict):
    """A write_rules_iterative stand-in recording the force_keyless kwarg."""

    def write_rules_iterative(table, invariants, source_path, output_dir=None,
                              *, max_iterations=3, certora_args=None,
                              force_keyless=False, **kwargs):
        record["iter_called"] = True
        record["force_keyless"] = force_keyless
        return {
            "final_spec": "rule r { assert true; }",
            "iterations": [{"iteration": 1}],
            "final_report": {},
        }

    return write_rules_iterative


def _load_pipeline_with_spies(verify_spy, iterative_spy):
    """Load pipeline.py with the verifier + iterative stage replaced by spies."""
    spies = {
        "spec_pipeline.stage2_invariants": {
            "mine_invariants": lambda *a, **k: {}
        },
        "spec_pipeline.stage3_rules": {"write_rules": lambda *a, **k: ""},
        "spec_pipeline.stage3_iterative": {
            "write_rules_iterative": iterative_spy
        },
        "spec_pipeline.stage4_critic": {
            "criticize": lambda *a, **k: [],
            "apply_findings": lambda *a, **k: "",
        },
        "spec_pipeline.stage5_verify": {"verify_with_prover": verify_spy},
    }
    saved: dict[str, object] = {}
    for name, attrs in spies.items():
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod
    unique = "spec_pipeline._pipeline_under_test_typecheck_only"
    try:
        sys.modules.pop(unique, None)
        return _load_module_by_path(unique, "spec_pipeline/pipeline.py")
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


def _one_contract_table() -> Stage1Table:
    table = Stage1Table(project_path="/proj")
    table.contracts["Token"] = FirstPartyContract(
        name="Token", kind="contract", source_file="Token.sol"
    )
    return table


# ---------------------------------------------------------------------------
# run_pipeline: typecheck_only forces keyless stage3, runs stage5 typecheck-only,
# maps the clean result to exit 0, and NEVER calls the cloud.
# ---------------------------------------------------------------------------


def test_run_pipeline_typecheck_only_exits_zero_and_no_cloud(monkeypatch, tmp_path):
    rec: dict = {}
    verify_spy = _make_verify_spy(rec)
    iter_spy = _make_iterative_spy(rec)
    pipeline = _load_pipeline_with_spies(verify_spy, iter_spy)

    # Stage 1 extractor returns a one-contract table (no slither).
    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _one_contract_table()
    )

    # A key present must NOT cause a cloud proof in typecheck_only mode.
    monkeypatch.setenv("CERTORAKEY", "present-but-ignored")

    sol = tmp_path / "Token.sol"
    sol.write_text("contract Token {}")

    results = pipeline.run_pipeline(
        sol, output_dir=tmp_path / "out", stages=[1, 2, 3, 4, 5],
        typecheck_only=True,
    )

    # Stage 3 iterated in forced-keyless mode.
    assert rec.get("iter_called") is True
    assert rec.get("force_keyless") is True
    # Stage 5 ran typecheck-only; the cloud verdict path was never taken.
    assert rec.get("called") is True
    assert rec.get("typecheck_only") is True
    # The clean local typecheck maps to a zero-exit success outcome.
    assert results["outcome"] == "typecheck_passed"
    assert results["exit_code"] == 0


def test_run_pipeline_typecheck_only_failed_maps_to_exit_six(monkeypatch, tmp_path):
    rec: dict = {}

    def verify_fail(source_path, cvl_spec, output_dir, certora_args=None, *,
                    table=None, run_local_typecheck=False, typecheck_only=False,
                    **kwargs):
        rec["typecheck_only"] = typecheck_only
        assert typecheck_only is True
        return {
            "status": "typecheck_failed",
            "summary": {"total_rules": 0, "passed": 0, "vacuous": 0,
                        "failed": 0, "dead": 0, "timeout": 0, "pass_rate": None},
            "rules": [],
            "local_typecheck_passed": False,
        }

    pipeline = _load_pipeline_with_spies(verify_fail, _make_iterative_spy(rec))
    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _one_contract_table()
    )

    sol = tmp_path / "Token.sol"
    sol.write_text("contract Token {}")

    results = pipeline.run_pipeline(
        sol, output_dir=tmp_path / "out", stages=[1, 2, 3, 4, 5],
        typecheck_only=True,
    )
    # typecheck_failed -> exit 6.
    assert results["outcome"] == "typecheck_failed"
    assert results["exit_code"] == 6


def test_run_pipeline_typecheck_only_implies_iterative_stage3(monkeypatch, tmp_path):
    """Passing typecheck_only WITHOUT iterative_stage3 still forces the keyless
    iterative loop (the ergonomics requirement)."""
    rec: dict = {}
    pipeline = _load_pipeline_with_spies(_make_verify_spy(rec), _make_iterative_spy(rec))
    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _one_contract_table()
    )

    sol = tmp_path / "Token.sol"
    sol.write_text("contract Token {}")

    pipeline.run_pipeline(
        sol, output_dir=tmp_path / "out", stages=[1, 2, 3, 4, 5],
        typecheck_only=True,  # iterative_stage3 defaults False
    )
    # The iterative loop ran despite iterative_stage3 not being passed.
    assert rec.get("iter_called") is True
    assert rec.get("force_keyless") is True


# ---------------------------------------------------------------------------
# CLI wiring (ast-level): --typecheck-only is defined and threaded into both
# run_pipeline(...) and run_single_stage(...).
# ---------------------------------------------------------------------------

_CLI_PATH = _REPO_ROOT / "spec_pipeline" / "cli.py"
_CLI_TREE = ast.parse(_CLI_PATH.read_text())


def test_cli_defines_typecheck_only_flag():
    """cli.py registers a ``--typecheck-only`` argparse flag."""
    literals = {
        node.value
        for node in ast.walk(_CLI_TREE)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "--typecheck-only" in literals


def _call_keywords(func_name: str) -> list[set[str]]:
    """Return the keyword-name sets of every call to *func_name* in cli.py."""
    out: list[set[str]] = []
    for node in ast.walk(_CLI_TREE):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == func_name:
                out.append({kw.arg for kw in node.keywords if kw.arg})
    return out


def test_cli_threads_typecheck_only_into_run_pipeline():
    calls = _call_keywords("run_pipeline")
    assert calls, "expected a run_pipeline(...) call in cli.py"
    assert all("typecheck_only" in kws for kws in calls)


def test_cli_threads_typecheck_only_into_run_single_stage():
    calls = _call_keywords("run_single_stage")
    assert calls, "expected a run_single_stage(...) call in cli.py"
    assert all("typecheck_only" in kws for kws in calls)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
