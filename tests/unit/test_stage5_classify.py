"""Unit tests for the honest Verification_Status classifier and Spec_Bundle
header (task 7.2, Requirements 20.2, 20.3, 20.5, 20.6, 20.11).

These tests drive :func:`_classify_status` with synthetic parsed prover outputs
(rule verdict lists, warnings, declared-rule sets, typecheck diagnostics) — no
real certoraRun. They also exercise :func:`spec_bundle_header` and
:func:`verify_with_prover`'s header writing.

``spec_pipeline/__init__.py`` eagerly imports slither-backed stages, so —
mirroring ``test_stage5_verify.py`` — ``stage5_verify.py`` is loaded directly
via importlib to stay toolchain-free.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

try:  # normal path once optional native deps are present
    from spec_pipeline import stage5_verify as S  # type: ignore
except Exception:  # pragma: no cover - fallback when slither absent
    _MOD_NAME = "spec_pipeline_stage5_verify_classify_under_test"
    _PATH = Path(__file__).resolve().parents[2] / "spec_pipeline" / "stage5_verify.py"
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _PATH)
    S = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = S
    _spec.loader.exec_module(S)


def _rules(*pairs):
    return [{"name": n, "status": s} for n, s in pairs]


# ---------------------------------------------------------------------------
# classifier semantics (R20.2, R20.3, R20.11)
# ---------------------------------------------------------------------------


def test_verified_when_every_declared_rule_passes_no_vacuous_no_warnings():
    rules = _rules(("r1", "PASSED"), ("r2", "PASSED"))
    status = S._classify_status(
        rules,
        tool_available=True,
        declared_rules={"r1", "r2"},
        warnings=[],
    )
    assert status == "verified"


def test_verified_with_warnings_when_all_pass_but_warning_present():
    rules = _rules(("r1", "PASSED"))
    status = S._classify_status(
        rules,
        tool_available=True,
        declared_rules={"r1"},
        warnings=["a prover warning"],
    )
    assert status == "verified_with_warnings"


def test_vacuous_when_all_pass_but_one_vacuous():
    rules = _rules(("r1", "PASSED"), ("r2", "VACUOUS"))
    status = S._classify_status(
        rules, tool_available=True, declared_rules={"r1", "r2"}, warnings=[]
    )
    assert status == "vacuous"


def test_violated_when_a_rule_fails():
    rules = _rules(("r1", "PASSED"), ("r2", "FAILED"))
    status = S._classify_status(
        rules, tool_available=True, declared_rules={"r1", "r2"}
    )
    assert status == "violated"


def test_timeout_when_a_rule_times_out_and_none_fail():
    rules = _rules(("r1", "PASSED"), ("r2", "TIMEOUT"))
    status = S._classify_status(
        rules, tool_available=True, declared_rules={"r1", "r2"}
    )
    assert status == "timeout"


def test_typecheck_failed_on_diagnostics():
    status = S._classify_status(
        [], tool_available=True, typecheck_diagnostics=[{"line": 4, "message": "bad"}]
    )
    assert status == "typecheck_failed"


def test_typecheck_failed_on_prover_error_verdict():
    rules = _rules(("r1", "ERROR"))
    status = S._classify_status(rules, tool_available=True, declared_rules={"r1"})
    assert status == "typecheck_failed"


def test_tool_unavailable_short_circuits():
    assert S._classify_status([], tool_available=False) == "tool_unavailable"


def test_declared_rule_without_verdict_is_not_verified():
    # r2 is declared but the prover returned no verdict for it -> NOT verified.
    rules = _rules(("r1", "PASSED"))
    status = S._classify_status(
        rules, tool_available=True, declared_rules={"r1", "r2"}, warnings=[]
    )
    assert status == "not_run"


def test_zero_declared_rules_cannot_be_verified():
    # No declared rules => rule count is zero => R20.11 forbids verified.
    status = S._classify_status(
        [], tool_available=True, declared_rules=set(), warnings=[]
    )
    assert status != "verified"


# ---------------------------------------------------------------------------
# declared-rule parsing (R20.2)
# ---------------------------------------------------------------------------


def test_declared_rule_names_parses_rules_and_invariants():
    spec = """
    rule transferPreservesTotal(env e) { assert true; }
    invariant totalIsPositive() total() > 0;
    rule anotherOne { assert true; }
    """
    names = S._declared_rule_names(spec)
    assert names == {"transferPreservesTotal", "totalIsPositive", "anotherOne"}


# ---------------------------------------------------------------------------
# honest invariant (R20.11)
# ---------------------------------------------------------------------------


def test_is_verified_honest_rejects_inconsistent_verified_report():
    bad = {"status": "verified", "rules": [{"name": "r1", "status": "VACUOUS"}], "warnings": []}
    assert not S.is_verified_honest(bad)

    empty = {"status": "verified", "rules": [], "warnings": []}
    assert not S.is_verified_honest(empty)

    warned = {"status": "verified", "rules": [{"name": "r1", "status": "PASSED"}], "warnings": ["w"]}
    assert not S.is_verified_honest(warned)

    good = {"status": "verified", "rules": [{"name": "r1", "status": "PASSED"}], "warnings": []}
    assert S.is_verified_honest(good)


# ---------------------------------------------------------------------------
# Spec_Bundle header (R20.6)
# ---------------------------------------------------------------------------


def test_spec_bundle_header_contains_status_runid_version_and_counts():
    report = {
        "status": "verified",
        "rules": [{"name": "r1", "status": "PASSED"}, {"name": "r2", "status": "PASSED"}],
        "warnings": [],
        "invocation": {"argv": ["certoraRun", "x"], "certora_version": "7.1.0"},
    }
    header = S.spec_bundle_header(report, run_id="run-2024-abc")

    assert header.startswith("//")
    assert "Verification_Status: verified" in header
    assert "Run_Id: run-2024-abc" in header
    assert "certoraRun_version: 7.1.0" in header
    assert "PASSED=2" in header


def test_spec_bundle_header_omits_counts_when_tool_absent():
    report = S._tool_unavailable_report(["/usr/bin/certoraRun"])
    header = S.spec_bundle_header(report, run_id="run-1")

    assert "Verification_Status: tool_unavailable" in header
    assert "certoraRun_version: unavailable" in header
    # No per-verdict counts written as zeros.
    assert "PASSED=0" not in header
    assert "=0" not in header
    assert "unavailable (certoraRun not invoked)" in header


def test_spec_bundle_header_names_vacuous_rules():
    report = {
        "status": "vacuous",
        "rules": [{"name": "r1", "status": "PASSED"}, {"name": "vac", "status": "VACUOUS"}],
        "warnings": [],
        "invocation": {"argv": ["certoraRun"], "certora_version": "7.0"},
    }
    header = S.spec_bundle_header(report, run_id="r")
    assert "Vacuous_Rules: vac" in header


# ---------------------------------------------------------------------------
# end-to-end header writing through verify_with_prover
# ---------------------------------------------------------------------------


class _FakeContract:
    def __init__(self, inheritance=None):
        self.inheritance = list(inheritance or [])


class _FakeTable:
    def __init__(self, contracts):
        self.contracts = contracts


def _sol(tmp_path: Path) -> Path:
    p = tmp_path / "src" / "Vault.sol"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("pragma solidity ^0.8.0;\ncontract Vault {}\n")
    return p


def test_verify_writes_bundle_spec_with_header(tmp_path):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    table = _FakeTable({"Vault": _FakeContract()})

    def runner(cmd, capture_output=True, text=True, timeout=None, cwd=None):
        return subprocess.CompletedProcess(cmd, 0, "Rule 'r1' PASSED\n", "")

    report = S.verify_with_prover(
        src,
        "rule r1 { assert true; }",
        output_dir=out,
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        run_id="run-xyz",
    )

    bundle = Path(report["bundle_spec_path"])
    assert bundle.is_file()
    content = bundle.read_text()
    assert content.startswith("// ---- Spec_Bundle ----")
    assert "Run_Id: run-xyz" in content
    assert "rule r1 { assert true; }" in content
    # r1 was declared and got a verdict -> verified.
    assert report["status"] == "verified"
    assert report["declared_rules"] == ["r1"]


def test_verify_declared_rule_without_verdict_not_verified_e2e(tmp_path):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    table = _FakeTable({"Vault": _FakeContract()})

    def runner(cmd, capture_output=True, text=True, timeout=None, cwd=None):
        # Only r1 gets a verdict; r2 is declared but silent.
        return subprocess.CompletedProcess(cmd, 0, "Rule 'r1' PASSED\n", "")

    report = S.verify_with_prover(
        src,
        "rule r1 { assert true; } rule r2 { assert true; }",
        output_dir=out,
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        run_id="run-xyz",
    )
    assert report["status"] != "verified"
    assert set(report["declared_rules"]) == {"r1", "r2"}


def test_tool_unavailable_still_writes_header(tmp_path, monkeypatch):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    table = _FakeTable({"Vault": _FakeContract()})

    # Force certoraRun ABSENT hermetically (R10.2): the venv now ships it in the
    # interpreter bin dir (searched before PATH), so ``certora_bin=None`` no
    # longer resolves absent on its own; pin the discovery seam to None so the
    # tool_unavailable header is exercised whether or not the tool is installed.
    monkeypatch.setattr(S, "_find_certora_bin", lambda: None)

    report = S.verify_with_prover(
        src,
        "rule r1 { assert true; }",
        output_dir=out,
        table=table,
        certora_bin=None,
        run_id="run-abc",
    )
    assert report["status"] == "tool_unavailable"
    bundle = Path(report["bundle_spec_path"])
    assert bundle.is_file()
    content = bundle.read_text()
    assert "Verification_Status: tool_unavailable" in content
    assert "Run_Id: run-abc" in content


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
