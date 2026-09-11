"""Unit tests for the Repair_Loop selection + stop logic (task 12.6, R22).

These exercise the pure, slither-free decision helpers in
``spec_pipeline.stage3_iterative`` -- ``_select_best_iteration``,
``_passing_non_vacuous_count``, ``_diagnostics_repeat``, and the loop's
once-per-iteration / tool-unavailable behavior -- WITHOUT invoking slither,
certoraRun, or an LLM.

slither is not installable here, so (as in ``test_pipeline_zero_contract.py``)
we register slither-free stubs for ``solidity_graph.analyzer`` and the parent
packages BEFORE loading ``spec_pipeline.stage3_iterative`` (whose
``from .stage1_extract import Stage1Table`` eagerly pulls the analyzer). Every
other import of ``stage3_iterative`` (cvl, methods_block, prompts, utils,
llm_client) is already slither-free, so once the analyzer stub is in place the
whole module loads by file path via importlib.
"""

from __future__ import annotations

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

if "spec_pipeline.stage3_iterative" not in sys.modules:
    itr = _load_module_by_path(
        "spec_pipeline.stage3_iterative", "spec_pipeline/stage3_iterative.py"
    )
else:  # pragma: no cover - depends on collection order
    itr = sys.modules["spec_pipeline.stage3_iterative"]


# ---------------------------------------------------------------------------
# Iteration record builders
# ---------------------------------------------------------------------------


def _rules(*statuses: str) -> list[dict]:
    return [{"name": f"r{i}", "status": s} for i, s in enumerate(statuses)]


def _iteration(num: int, *, status: str = "violated", rules=None, diagnostics=None):
    rules = rules if rules is not None else []
    rec = {
        "iteration": num,
        "spec": f"spec-{num}",
        "rules": rules,
        "status": status,
        "diagnostics": diagnostics or [],
    }
    rec["passing_non_vacuous_count"] = itr._passing_non_vacuous_count(rec)
    rec["typecheck_accepted"] = itr._typecheck_accepted(rec)
    return rec


# ---------------------------------------------------------------------------
# _passing_non_vacuous_count (R22.1, R22.8)
# ---------------------------------------------------------------------------


def test_passing_count_counts_only_passed_rules():
    rec = _iteration(1, status="violated", rules=_rules("PASSED", "PASSED", "FAILED"))
    assert itr._passing_non_vacuous_count(rec) == 2


def test_passing_count_excludes_vacuous():
    rec = _iteration(1, status="vacuous", rules=_rules("PASSED", "VACUOUS"))
    assert itr._passing_non_vacuous_count(rec) == 1


@pytest.mark.parametrize("status", ["typecheck_failed", "tool_unavailable", "not_run"])
def test_no_verdict_status_counts_as_zero(status):
    # Even if a rule list is present, a no-verdict status yields zero (R22.8).
    rec = _iteration(1, status=status, rules=_rules("PASSED", "PASSED"))
    assert itr._passing_non_vacuous_count(rec) == 0


# ---------------------------------------------------------------------------
# best-not-last selection (R22.1)
# ---------------------------------------------------------------------------


def test_selects_best_not_last_iteration():
    iterations = [
        _iteration(1, status="verified", rules=_rules("PASSED", "PASSED", "PASSED")),
        _iteration(2, status="violated", rules=_rules("PASSED")),
        _iteration(3, status="violated", rules=_rules("PASSED", "FAILED")),
    ]
    index, reason = itr._select_best_iteration(iterations)
    assert index == 0
    assert "iteration 1" in reason


# ---------------------------------------------------------------------------
# tie -> earliest (R22.2)
# ---------------------------------------------------------------------------


def test_tie_returns_earliest_iteration():
    iterations = [
        _iteration(1, status="violated", rules=_rules("PASSED", "PASSED", "FAILED")),
        _iteration(2, status="violated", rules=_rules("PASSED", "PASSED", "FAILED")),
    ]
    index, reason = itr._select_best_iteration(iterations)
    assert index == 0
    assert iterations[index]["iteration"] == 1


# ---------------------------------------------------------------------------
# typecheck regression returns earlier spec + records both numbers (R22.3)
# ---------------------------------------------------------------------------


def test_typecheck_regression_returns_earlier_accepted():
    iterations = [
        _iteration(1, status="violated", rules=_rules("PASSED")),
        # Iteration 2 would have MORE passing rules if it counted, but it is a
        # typecheck rejection following an accepted iteration -> return #1.
        _iteration(2, status="typecheck_failed", rules=_rules("PASSED", "PASSED")),
    ]
    index, reason = itr._select_best_iteration(iterations)
    assert index == 0
    assert "regression" in reason
    # Both iteration numbers named (R22.3).
    assert "1" in reason and "2" in reason


def test_typecheck_regression_first_iteration_rejected_no_regression():
    # No earlier accepted iteration exists, so it is not a regression; fall
    # through to best-by-count (both count zero -> earliest).
    iterations = [
        _iteration(1, status="typecheck_failed", rules=[]),
        _iteration(2, status="violated", rules=_rules("PASSED")),
    ]
    index, _ = itr._select_best_iteration(iterations)
    assert index == 1  # iteration 2 has the higher passing count


# ---------------------------------------------------------------------------
# repeated diagnostic set equality (R22.10)
# ---------------------------------------------------------------------------


def test_diagnostics_repeat_is_order_independent():
    prev = [
        {"category": "solidity_pragma", "line": 3, "message": "m1"},
        {"category": "no_cvl", "line": 1, "message": "m2"},
    ]
    curr = [
        {"category": "no_cvl", "line": 1, "message": "m2"},
        {"category": "solidity_pragma", "line": 3, "message": "m1"},
    ]
    assert itr._diagnostics_repeat(prev, curr) is True


def test_diagnostics_repeat_false_when_differ():
    prev = [{"category": "no_cvl", "line": 1, "message": "m2"}]
    curr = [{"category": "no_cvl", "line": 2, "message": "m2"}]
    assert itr._diagnostics_repeat(prev, curr) is False


def test_diagnostics_repeat_accepts_cvldiagnostic_objects():
    prev = [itr.CVLDiagnostic("no_cvl", 1, "m")]
    curr = [itr.CVLDiagnostic("no_cvl", 1, "m")]
    assert itr._diagnostics_repeat(prev, curr) is True


# ---------------------------------------------------------------------------
# all-pass / vacuous helpers (R22.9)
# ---------------------------------------------------------------------------


def test_all_rules_passed_requires_nonempty_and_all_passed():
    assert itr._all_rules_passed(_iteration(1, rules=_rules("PASSED", "PASSED")))
    assert not itr._all_rules_passed(_iteration(1, rules=_rules("PASSED", "FAILED")))
    assert not itr._all_rules_passed(_iteration(1, rules=[]))


def test_has_vacuous_detects_vacuous_and_dead():
    assert itr._has_vacuous(_iteration(1, rules=_rules("PASSED", "VACUOUS")))
    assert itr._has_vacuous(_iteration(1, rules=_rules("DEAD")))
    assert not itr._has_vacuous(_iteration(1, rules=_rules("PASSED")))


def test_clamp_max_iterations_range():
    assert itr._clamp_max_iterations(3) == 3
    assert itr._clamp_max_iterations(0) == 1
    assert itr._clamp_max_iterations(99) == 10
    assert itr._clamp_max_iterations(7) == 7


# ---------------------------------------------------------------------------
# Loop-level: certoraRun once per iteration; no extra post-loop call
# ---------------------------------------------------------------------------


class _FakeLLM:
    def call(self, system, user, temperature=0.2):
        return "```cvl\nrule r1 { assert true; }\n```"


class _Table:
    """Minimal duck-typed Stage 1 table for the loop."""

    def __init__(self):
        self.contracts = {"Token": object()}

    def to_text(self):
        return "TABLE"


def _install_loop_fakes(monkeypatch, reports):
    """Patch the loop's collaborators; return a counter of verify calls."""
    calls = {"verify": 0}

    def fake_verify(*a, **k):
        idx = calls["verify"]
        calls["verify"] += 1
        return reports[min(idx, len(reports) - 1)]

    monkeypatch.setattr(itr, "verify_with_prover", fake_verify)
    # These loop tests exercise the CLOUD verdict path. Force the keyless local
    # typecheck to report unavailable so the loop falls back to the injected
    # verify_with_prover exactly as the pre-keyless loop did (the keyless
    # typecheck signal has its own dedicated tests).
    monkeypatch.setattr(
        itr, "local_typecheck",
        lambda *a, **k: {
            "typecheck_passed": False,
            "errors": [],
            "raw_tail": "",
            "status": "typecheck_unavailable",
            "reason": "test: local typecheck disabled",
        },
    )
    monkeypatch.setattr(itr, "generate_methods_block",
                        lambda *a, **k: types.SimpleNamespace(text="methods {}"))
    monkeypatch.setattr(itr, "_read_source", lambda *a, **k: "contract Token {}")
    # Distinct diagnostics per call so the repeated-set stop does not fire; the
    # counter drives a unique line number each iteration.
    diag_calls = {"n": 0}

    def fake_validate(*a, **k):
        diag_calls["n"] += 1
        return [itr.CVLDiagnostic("no_cvl", diag_calls["n"], "m")]

    monkeypatch.setattr(itr, "validate_cvl", fake_validate)
    return calls


def _report(status, *statuses, summary=None):
    rules = _rules(*statuses)
    return {
        "status": status,
        "rules": rules,
        "summary": summary or {"total_rules": len(rules)},
    }


def test_verify_called_once_per_iteration_no_post_loop_call(monkeypatch, tmp_path):
    # Three iterations, none fully passing -> loop runs to max_iterations=3.
    reports = [
        _report("violated", "PASSED", "FAILED"),
        _report("violated", "PASSED", "FAILED"),
        _report("violated", "PASSED", "FAILED"),
    ]
    calls = _install_loop_fakes(monkeypatch, reports)

    sol = tmp_path / "Token.sol"
    sol.write_text("contract Token {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path,
        llm_client=_FakeLLM(), max_iterations=3,
    )

    # Exactly one certoraRun invocation per iteration, and NO extra post-loop
    # call (R22.5, R22.6): 3 iterations -> exactly 3 verify calls.
    assert calls["verify"] == 3
    assert len(result["iterations"]) == 3
    # final_report is the selected iteration's report summary (reused, R22.5).
    assert result["final_report"] == result["iterations"][
        result["selected_iteration"] - 1
    ]["report"]


def test_tool_unavailable_stops_and_records_cause(monkeypatch, tmp_path):
    reports = [
        {
            "status": "tool_unavailable",
            "rules": [],
            "summary": {"total_rules": None},
            "note": "certoraRun not available",
            "searched_paths": ["/usr/bin/certoraRun"],
        },
    ]
    calls = _install_loop_fakes(monkeypatch, reports)

    sol = tmp_path / "Token.sol"
    sol.write_text("contract Token {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path,
        llm_client=_FakeLLM(), max_iterations=3,
    )

    # Stopped after the first iteration (prover uninvokable, R22.11).
    assert calls["verify"] == 1
    assert len(result["iterations"]) == 1
    it0 = result["iterations"][0]
    assert it0["status"] == "tool_unavailable"
    assert it0["passing_non_vacuous_count"] == 0
    assert it0["blocking_cause"] == "certoraRun not available"
    assert result["stop_cause"].startswith("prover uninvokable")


def test_all_pass_zero_vacuous_stops_early(monkeypatch, tmp_path):
    reports = [
        _report("verified", "PASSED", "PASSED"),
        _report("violated", "FAILED"),  # would run if not stopped
    ]
    calls = _install_loop_fakes(monkeypatch, reports)

    sol = tmp_path / "Token.sol"
    sol.write_text("contract Token {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path,
        llm_client=_FakeLLM(), max_iterations=3,
    )

    assert calls["verify"] == 1  # stopped after all-pass (R22.9)
    assert result["stop_cause"] == "all rules passing, zero vacuous"
    assert result["selected_iteration"] == 1


def test_repeated_diagnostics_stops(monkeypatch, tmp_path):
    reports = [
        _report("violated", "PASSED", "FAILED"),
        _report("violated", "PASSED", "FAILED"),
        _report("violated", "PASSED", "FAILED"),
    ]
    calls = _install_loop_fakes(monkeypatch, reports)
    # Same diagnostic set every iteration.
    monkeypatch.setattr(
        itr, "validate_cvl",
        lambda *a, **k: [itr.CVLDiagnostic("no_cvl", 1, "m")],
    )

    sol = tmp_path / "Token.sol"
    sol.write_text("contract Token {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path,
        llm_client=_FakeLLM(), max_iterations=3,
    )

    # Iteration 1 records diagnostics; iteration 2 repeats them -> stop after 2.
    assert calls["verify"] == 2
    assert result["stop_cause"] == "repeated CVL diagnostic set"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
