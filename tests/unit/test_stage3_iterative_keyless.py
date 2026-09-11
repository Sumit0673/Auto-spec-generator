"""Unit tests for the KEYLESS iterative Repair_Loop (Task B).

Exercises ``write_rules_iterative`` driven by the keyless local CVL typecheck as
the per-iteration feedback signal: an injected local-typecheck function that
FAILS on the first iteration (returning a concrete ``file:line:col: message``
diagnostic) and PASSES on the second. The loop must feed the typechecker errors
back into the feedback prompt, and declare a PASS on the clean iteration.

Fully offline: slither-free stubs are registered before loading
``spec_pipeline.stage3_iterative`` (mirroring
``test_stage3_iterative_selection.py``); the LLM, local typecheck, and cloud
verifier are all injected. CERTORAKEY is never set.
"""

from __future__ import annotations

import importlib.util
import re
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
else:  # pragma: no cover
    itr = sys.modules["spec_pipeline.stage3_iterative"]


class _Table:
    def __init__(self):
        self.contracts = {"SimpleVault": object()}

    def to_text(self):
        return "TABLE"


class _RecordingLLM:
    """Records the user prompts it is called with, returns a fixed spec."""

    def __init__(self):
        self.prompts = []

    def call(self, system, user, temperature=0.2):
        self.prompts.append(user)
        return "```cvl\nrule r { assert true; }\n```"


def _install_common_fakes(monkeypatch):
    monkeypatch.setattr(itr, "generate_methods_block",
                        lambda *a, **k: types.SimpleNamespace(text="methods {}"))
    monkeypatch.setattr(itr, "_read_source", lambda *a, **k: "contract SimpleVault {}")
    # Distinct diagnostics per call so the repeated-set stop does not fire.
    diag_calls = {"n": 0}

    def fake_validate(*a, **k):
        diag_calls["n"] += 1
        return [itr.CVLDiagnostic("no_cvl", diag_calls["n"], "m")]

    monkeypatch.setattr(itr, "validate_cvl", fake_validate)
    # The cloud verifier must NOT be reached in keyless mode when the local
    # typecheck resolves the outcome.
    def no_cloud(*a, **k):
        raise AssertionError("verify_with_prover must not run in keyless success path")

    monkeypatch.setattr(itr, "verify_with_prover", no_cloud)
    # No key -> keyless.
    monkeypatch.delenv("CERTORAKEY", raising=False)


def test_keyless_loop_feeds_typecheck_errors_and_passes_on_clean(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch)

    # Local typecheck: FAIL first (with a sig: selector error), then PASS.
    tc_calls = {"n": 0}

    def fake_local_typecheck(source_path, cvl_spec, output_dir, *, table=None, **k):
        tc_calls["n"] += 1
        if tc_calls["n"] == 1:
            return {
                "typecheck_passed": False,
                "status": "typecheck_failed",
                "errors": [
                    {
                        "file": "SimpleVault.spec",
                        "line": 26,
                        "col": 37,
                        "message": (
                            "Variable `bool` has not been declared. Did you "
                            "forget to use `sig:` for a method selector?"
                        ),
                    }
                ],
                "raw_tail": "CVL syntax or type check failed",
            }
        return {
            "typecheck_passed": True,
            "status": "typecheck_passed",
            "errors": [],
            "raw_tail": "CVL type checking passed",
        }

    monkeypatch.setattr(itr, "local_typecheck", fake_local_typecheck)

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path,
        llm_client=llm, max_iterations=3,
    )

    # Ran exactly two iterations: fail then clean pass.
    assert tc_calls["n"] == 2
    assert len(result["iterations"]) == 2
    # First iteration recorded a typecheck failure with the concrete error.
    it0 = result["iterations"][0]
    assert it0["status"] == "typecheck_failed"
    assert it0["typecheck_errors"][0]["line"] == 26
    assert it0["typecheck_clean"] is False
    # Second iteration typechecked clean -> selected + declared pass.
    it1 = result["iterations"][1]
    assert it1["typecheck_clean"] is True
    assert result["stop_cause"] == "local typecheck passed (keyless)"
    assert result["selected_iteration"] == 2
    assert result["final_spec"] == it1["spec"]

    # The concrete typechecker error was fed into the SECOND (feedback) prompt.
    assert len(llm.prompts) == 2
    feedback_prompt = llm.prompts[1]
    assert "CVL TYPECHECKER ERRORS" in feedback_prompt
    assert "SimpleVault.spec:26:37" in feedback_prompt
    assert "sig:" in feedback_prompt


def test_keyless_loop_first_iteration_clean_passes_immediately(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch)

    monkeypatch.setattr(
        itr, "local_typecheck",
        lambda *a, **k: {
            "typecheck_passed": True,
            "status": "typecheck_passed",
            "errors": [],
            "raw_tail": "",
        },
    )

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=3,
    )

    assert len(result["iterations"]) == 1
    assert result["selected_iteration"] == 1
    assert result["stop_cause"] == "local typecheck passed (keyless)"
    # Only the initial (non-feedback) prompt was issued.
    assert len(llm.prompts) == 1


def test_keyless_loop_all_failing_runs_to_max_iterations(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch)

    # Local typecheck fails every iteration with a DISTINCT error (line changes)
    # so the repeated-diagnostic stop is driven by validate_cvl, not this.
    tc_calls = {"n": 0}

    def always_fail(source_path, cvl_spec, output_dir, *, table=None, **k):
        tc_calls["n"] += 1
        return {
            "typecheck_passed": False,
            "status": "typecheck_failed",
            "errors": [
                {"file": "SimpleVault.spec", "line": tc_calls["n"], "col": 1,
                 "message": f"error {tc_calls['n']}"}
            ],
            "raw_tail": "CVL syntax or type check failed",
        }

    monkeypatch.setattr(itr, "local_typecheck", always_fail)

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=3,
    )

    assert tc_calls["n"] == 3
    assert len(result["iterations"]) == 3
    assert result["stop_cause"].startswith("reached configured max iterations")
    # Every iteration is a typecheck failure; selection falls to earliest (all
    # tie at zero passing rules).
    assert all(it["status"] == "typecheck_failed" for it in result["iterations"])
    assert result["selected_iteration"] == 1


# ---------------------------------------------------------------------------
# setup_failed: certoraRun failed BEFORE CVL typechecking (e.g. unknown
# --verify target). The loop must STOP immediately with an honest stop_cause,
# NOT stall on "repeated typecheck errors", and NOT call the cloud verifier.
# ---------------------------------------------------------------------------


def test_keyless_loop_setup_failed_stops_immediately(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch)  # verify_with_prover raises if called

    tc_calls = {"n": 0}

    def setup_fail(source_path, cvl_spec, output_dir, *, table=None, **k):
        tc_calls["n"] += 1
        return {
            "typecheck_passed": False,
            "status": "setup_failed",
            "errors": [],
            "raw_tail": (
                "'verify' argument, GovernorBravoDelegateStorageV1, "
                "doesn't match any contract name"
            ),
            "reason": (
                "'verify' argument, GovernorBravoDelegateStorageV1, "
                "doesn't match any contract name"
            ),
        }

    monkeypatch.setattr(itr, "local_typecheck", setup_fail)

    llm = _RecordingLLM()
    sol = tmp_path / "GovernorBravoInterfaces.sol"
    sol.write_text("contract A {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=3,
    )

    # Stopped on the FIRST iteration; the cloud verifier was never called
    # (_install_common_fakes wires verify_with_prover to raise).
    assert tc_calls["n"] == 1
    assert len(result["iterations"]) == 1
    assert result["stop_cause"].startswith("local typecheck could not run")
    assert "doesn't match any contract name" in result["stop_cause"]
    # NOT the misleading repeated-typecheck-errors stall.
    assert "repeated" not in result["stop_cause"]
    # The iteration record carries the reason so the history shows WHY it stopped.
    it0 = result["iterations"][0]
    assert it0["status"] == "setup_failed"
    assert "doesn't match any contract name" in it0["setup_failed_reason"]
    assert it0["typecheck_clean"] is False


# ---------------------------------------------------------------------------
# BUG 1: the keyless "repeated set" stop must compare the TYPECHECK error set,
# not the (constant) CVL_Validator diagnostics.
# ---------------------------------------------------------------------------


def _install_constant_validator(monkeypatch):
    """Common fakes but with a CONSTANT CVL_Validator diagnostic every call.

    This reproduces the real-run defect: the CVL_Validator keeps emitting the
    same `duplicated_methods_block` diagnostic while the typecheck errors change.
    The old code stopped on the constant diagnostic; the fixed code must ignore
    it in the keyless path and follow the changing typecheck errors instead.
    """
    monkeypatch.setattr(itr, "generate_methods_block",
                        lambda *a, **k: types.SimpleNamespace(text="methods {}"))
    monkeypatch.setattr(itr, "_read_source", lambda *a, **k: "contract SimpleVault {}")

    def constant_validator(*a, **k):
        return [itr.CVLDiagnostic("duplicated_methods_block", 1,
                                  "Methods block duplicates the generated methods block.")]

    monkeypatch.setattr(itr, "validate_cvl", constant_validator)

    def no_cloud(*a, **k):
        raise AssertionError("verify_with_prover must not run in keyless path")

    monkeypatch.setattr(itr, "verify_with_prover", no_cloud)
    monkeypatch.delenv("CERTORAKEY", raising=False)


def test_keyless_changing_typecheck_errors_run_to_max_despite_constant_diagnostic(
    monkeypatch, tmp_path
):
    """Typecheck errors that CHANGE each iteration must NOT early-stop on the
    constant CVL_Validator diagnostic -- the loop runs to max_iterations.

    This is the exact BUG 1 regression: constant `duplicated_methods_block`
    diagnostic, but the typecheck errors differ every iteration.
    """
    _install_constant_validator(monkeypatch)

    tc_calls = {"n": 0}

    # Each iteration reports a DIFFERENT typecheck error (different line/message),
    # mirroring iter1: 1 syntax error, iter2: different semantic errors, etc.
    error_sets = [
        [{"file": "SimpleVault.spec", "line": 13, "col": 5,
          "message": "syntax error near ';'"}],
        [{"file": "SimpleVault.spec", "line": 20, "col": 9,
          "message": "old() is not allowed here"},
         {"file": "SimpleVault.spec", "line": 22, "col": 3,
          "message": "parametric method call convention"}],
        [{"file": "SimpleVault.spec", "line": 31, "col": 1,
          "message": "unresolved reference foo"}],
        [{"file": "SimpleVault.spec", "line": 44, "col": 2,
          "message": "type mismatch"}],
        [{"file": "SimpleVault.spec", "line": 55, "col": 7,
          "message": "missing sig: selector"}],
    ]

    def changing_fail(source_path, cvl_spec, output_dir, *, table=None, **k):
        idx = tc_calls["n"]
        tc_calls["n"] += 1
        return {
            "typecheck_passed": False,
            "status": "typecheck_failed",
            "errors": error_sets[idx],
            "raw_tail": "CVL syntax or type check failed",
        }

    monkeypatch.setattr(itr, "local_typecheck", changing_fail)

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=5,
    )

    assert tc_calls["n"] == 5
    assert len(result["iterations"]) == 5
    # Stopped on max iterations, NOT on a repeated set.
    assert result["stop_cause"].startswith("reached configured max iterations")
    assert "repeated" not in result["stop_cause"]


def test_keyless_identical_typecheck_errors_stop_repeated(monkeypatch, tmp_path):
    """Typecheck errors IDENTICAL two iterations in a row => early-stop with a
    'repeated typecheck errors' stop_cause (a genuine stall)."""
    _install_constant_validator(monkeypatch)

    tc_calls = {"n": 0}

    def same_fail(source_path, cvl_spec, output_dir, *, table=None, **k):
        tc_calls["n"] += 1
        return {
            "typecheck_passed": False,
            "status": "typecheck_failed",
            "errors": [
                {"file": "SimpleVault.spec", "line": 13, "col": 5,
                 "message": "syntax error near ';'"}
            ],
            "raw_tail": "CVL syntax or type check failed",
        }

    monkeypatch.setattr(itr, "local_typecheck", same_fail)

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=5,
    )

    # Two iterations with identical typecheck errors -> stop on the second.
    assert tc_calls["n"] == 2
    assert len(result["iterations"]) == 2
    assert result["stop_cause"] == "repeated typecheck errors"


def test_keyless_identical_errors_reordered_still_stop(monkeypatch, tmp_path):
    """The repeat check is order-independent: the same errors in a different
    order still count as a repeat and stop the loop."""
    _install_constant_validator(monkeypatch)

    tc_calls = {"n": 0}
    a = {"file": "SimpleVault.spec", "line": 10, "col": 1, "message": "err A"}
    b = {"file": "SimpleVault.spec", "line": 20, "col": 2, "message": "err B"}

    def reordered_fail(source_path, cvl_spec, output_dir, *, table=None, **k):
        tc_calls["n"] += 1
        errs = [a, b] if tc_calls["n"] == 1 else [b, a]
        return {
            "typecheck_passed": False,
            "status": "typecheck_failed",
            "errors": list(errs),
            "raw_tail": "fail",
        }

    monkeypatch.setattr(itr, "local_typecheck", reordered_fail)

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=5,
    )

    assert tc_calls["n"] == 2
    assert result["stop_cause"] == "repeated typecheck errors"


# ---------------------------------------------------------------------------
# BUG 2: a response that already carries a methods block must NOT end up with
# two methods blocks (no spurious duplicated_methods_block).
# ---------------------------------------------------------------------------


class _MethodsLLM:
    """Returns a spec whose response already contains a `methods {` block."""

    def __init__(self):
        self.prompts = []

    def call(self, system, user, temperature=0.2):
        self.prompts.append(user)
        return (
            "```cvl\n"
            "methods {\n"
            "    function totalSupply() external returns (uint256) envfree;\n"
            "}\n\n"
            "rule r { assert true; }\n"
            "```"
        )


def test_keyless_response_with_methods_block_not_duplicated(monkeypatch, tmp_path):
    """When the LLM response already has a methods block, the loop must not
    prepend the generated one, so the final spec has exactly ONE methods block
    and the real CVL_Validator emits no duplicated_methods_block diagnostic."""
    # Use the REAL validate_cvl / extract_cvl so the duplication would surface.
    monkeypatch.setattr(
        itr, "generate_methods_block",
        lambda *a, **k: types.SimpleNamespace(
            text=("methods {\n"
                  "    function totalSupply() external returns (uint256) envfree;\n"
                  "}"),
        ),
    )
    monkeypatch.setattr(itr, "_read_source", lambda *a, **k: "contract SimpleVault {}")

    def no_cloud(*a, **k):
        raise AssertionError("verify_with_prover must not run in keyless path")

    monkeypatch.setattr(itr, "verify_with_prover", no_cloud)
    monkeypatch.delenv("CERTORAKEY", raising=False)

    # Typecheck passes immediately (we only care about the spec content).
    monkeypatch.setattr(
        itr, "local_typecheck",
        lambda *a, **k: {
            "typecheck_passed": True, "status": "typecheck_passed",
            "errors": [], "raw_tail": "",
        },
    )

    llm = _MethodsLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=3,
    )

    final_spec = result["final_spec"]
    # Exactly ONE methods block in the final spec: the generated block was NOT
    # prepended on top of the model's own methods block (the BUG 2 defect).
    assert len(re.findall(r"\bmethods\s*\{", final_spec)) == 1
    # The model's own methods block is preserved verbatim.
    assert "function totalSupply() external returns (uint256) envfree;" in final_spec


def test_keyless_response_without_methods_block_gets_prepended(monkeypatch, tmp_path):
    """Regression guard: when the response has NO methods block, the generated
    one is still prepended (one methods block appears in the final spec)."""
    monkeypatch.setattr(
        itr, "generate_methods_block",
        lambda *a, **k: types.SimpleNamespace(
            text=("methods {\n"
                  "    function totalSupply() external returns (uint256) envfree;\n"
                  "}"),
        ),
    )
    monkeypatch.setattr(itr, "_read_source", lambda *a, **k: "contract SimpleVault {}")
    monkeypatch.setattr(itr, "verify_with_prover",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no cloud")))
    monkeypatch.delenv("CERTORAKEY", raising=False)
    monkeypatch.setattr(
        itr, "local_typecheck",
        lambda *a, **k: {
            "typecheck_passed": True, "status": "typecheck_passed",
            "errors": [], "raw_tail": "",
        },
    )

    llm = _RecordingLLM()  # response has no methods block
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm, max_iterations=3,
    )
    assert len(re.findall(r"\bmethods\s*\{", result["final_spec"])) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# force_keyless: --typecheck-only forces the keyless path even when CERTORAKEY
# is set. The cloud verify_fn must NEVER be called; a clean local typecheck is
# the terminal pass.
# ---------------------------------------------------------------------------


def test_force_keyless_ignores_certora_key_and_stops_at_clean_typecheck(
    monkeypatch, tmp_path
):
    """Even with CERTORAKEY set, force_keyless=True keeps the loop keyless: the
    cloud verify_fn is never invoked and a clean local typecheck is the pass."""
    monkeypatch.setattr(itr, "generate_methods_block",
                        lambda *a, **k: types.SimpleNamespace(text="methods {}"))
    monkeypatch.setattr(itr, "_read_source", lambda *a, **k: "contract SimpleVault {}")

    diag_calls = {"n": 0}

    def fake_validate(*a, **k):
        diag_calls["n"] += 1
        return [itr.CVLDiagnostic("no_cvl", diag_calls["n"], "m")]

    monkeypatch.setattr(itr, "validate_cvl", fake_validate)
    # A key IS present -- force_keyless must still ignore it.
    monkeypatch.setenv("CERTORAKEY", "present-but-must-be-ignored")

    # Injected verify_fn RAISES if the cloud path is ever taken.
    def raising_verify(*a, **k):
        raise AssertionError(
            "cloud verify_fn must not run when force_keyless=True"
        )

    # Local typecheck: FAIL first, then PASS clean.
    tc_calls = {"n": 0}

    def fake_local_typecheck(source_path, cvl_spec, output_dir, *, table=None, **k):
        tc_calls["n"] += 1
        if tc_calls["n"] == 1:
            return {
                "typecheck_passed": False,
                "status": "typecheck_failed",
                "errors": [
                    {"file": "SimpleVault.spec", "line": 3, "col": 1,
                     "message": "bad"}
                ],
                "raw_tail": "CVL syntax or type check failed",
            }
        return {
            "typecheck_passed": True,
            "status": "typecheck_passed",
            "errors": [],
            "raw_tail": "CVL type checking passed",
        }

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm,
        max_iterations=3, force_keyless=True,
        local_typecheck_fn=fake_local_typecheck, verify_fn=raising_verify,
    )

    # Ran fail-then-pass; stopped at the clean keyless typecheck.
    assert tc_calls["n"] == 2
    assert result["stop_cause"] == "local typecheck passed (keyless)"
    assert result["selected_iteration"] == 2
    assert result["iterations"][1]["typecheck_clean"] is True


def test_force_keyless_first_iteration_clean_never_calls_cloud(monkeypatch, tmp_path):
    """A clean local typecheck on the first iteration is the pass; the cloud
    verify_fn is never called even though CERTORAKEY is set."""
    monkeypatch.setattr(itr, "generate_methods_block",
                        lambda *a, **k: types.SimpleNamespace(text="methods {}"))
    monkeypatch.setattr(itr, "_read_source", lambda *a, **k: "contract SimpleVault {}")
    monkeypatch.setattr(itr, "validate_cvl",
                        lambda *a, **k: [itr.CVLDiagnostic("no_cvl", 1, "m")])
    monkeypatch.setenv("CERTORAKEY", "present-but-ignored")

    def raising_verify(*a, **k):
        raise AssertionError("cloud verify_fn must not run when force_keyless=True")

    llm = _RecordingLLM()
    sol = tmp_path / "SimpleVault.sol"
    sol.write_text("contract SimpleVault {}")

    result = itr.write_rules_iterative(
        _Table(), {}, sol, output_dir=tmp_path, llm_client=llm,
        max_iterations=3, force_keyless=True,
        local_typecheck_fn=lambda *a, **k: {
            "typecheck_passed": True, "status": "typecheck_passed",
            "errors": [], "raw_tail": "",
        },
        verify_fn=raising_verify,
    )

    assert len(result["iterations"]) == 1
    assert result["selected_iteration"] == 1
    assert result["stop_cause"] == "local typecheck passed (keyless)"
    # Only the initial (non-feedback) prompt was issued.
    assert len(llm.prompts) == 1
