"""Unit tests for the Stage 5 Verifier (task 6.6).

Covers the four things this task owns (Requirements 8.1, 8.8-8.10, 8.13, 20.4,
20.7, 20.8):

* remappings and the selected solc reach the certoraRun argv, and the target is
  built from a contract name rather than the file / directory stem;
* an absent certoraRun yields Verification_Status ``tool_unavailable`` with a
  null pass rate and null per-verdict counts, plus the searched locations;
* an unknown ``--verify-contract`` name errors without invoking certoraRun;
* the parse source (structured / text) is recorded.

No real certoraRun or solc runs: a fake binary path and a fake subprocess runner
are injected. ``spec_pipeline/__init__.py`` eagerly imports slither-backed
stages, so — mirroring ``test_resolve.py`` — ``stage5_verify.py`` is loaded
directly via importlib to stay toolchain-free. (The module itself does not
import slither, but the package __init__ does.)
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
    _MOD_NAME = "spec_pipeline_stage5_verify_under_test"
    _PATH = Path(__file__).resolve().parents[2] / "spec_pipeline" / "stage5_verify.py"
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _PATH)
    S = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = S
    _spec.loader.exec_module(S)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeContract:
    def __init__(self, inheritance=None):
        self.inheritance = list(inheritance or [])


class _FakeTable:
    """Minimal stand-in for a Stage1Table: a ``contracts`` mapping."""

    def __init__(self, contracts):
        self.contracts = contracts


def _capturing_runner():
    """A fake subprocess runner that records the argv/cwd and returns a PASS."""
    captured = {}

    def run(cmd, capture_output=True, text=True, timeout=None, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="Rule 'r1' PASSED\n",
            stderr="",
        )

    return run, captured


def _sol(tmp_path: Path) -> Path:
    p = tmp_path / "src" / "Vault.sol"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("pragma solidity ^0.8.0;\ncontract Vault {}\n")
    return p


# ---------------------------------------------------------------------------
# (a) argv carries remappings + --solc and uses the contract name  (R8.1, R8.8)
# ---------------------------------------------------------------------------


def test_argv_includes_remappings_solc_and_contract_name(tmp_path):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    table = _FakeTable({"Vault": _FakeContract()})
    runner, captured = _capturing_runner()

    report = S.verify_with_prover(
        src,
        "rule r1 {}",
        output_dir=out,
        table=table,
        remappings=["@openzeppelin/=/deps/oz/", "solmate/=/deps/solmate/"],
        solc_path="/solc/0.8.19",
        project_root=tmp_path / "proj_root",
        certora_bin="/fake/certoraRun",
        runner=runner,
    )

    argv = captured["cmd"]
    # solc reaches the argv (R8.1).
    assert "--solc" in argv
    assert argv[argv.index("--solc") + 1] == "/solc/0.8.19"
    # remappings reach the argv (R8.1).
    assert "@openzeppelin/=/deps/oz/" in argv
    assert "solmate/=/deps/solmate/" in argv
    # Target is <ContractName>:<spec>, NOT the file stem or "project" (R8.8).
    verify_idx = argv.index("--verify")
    target = argv[verify_idx + 1]
    assert target.startswith("Vault:")
    assert not target.startswith("project:")
    # cwd is the resolved project root (R8.1).
    assert captured["cwd"] == str(tmp_path / "proj_root")
    # Invocation record captures argv, cwd, solc, remappings (R8.11 / design).
    inv = report["invocation"]
    assert inv["solc"] == "/solc/0.8.19"
    assert inv["cwd"] == str(tmp_path / "proj_root")
    assert "@openzeppelin/=/deps/oz/" in inv["remappings"]
    assert inv["argv"] == argv
    # A structured-less run records the text parse source (R20.8).
    assert report["parse_source"] == "text"


def test_remappings_and_solc_from_resolution_object(tmp_path):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    table = _FakeTable({"Vault": _FakeContract()})
    runner, captured = _capturing_runner()

    class _Solc:
        path = "/solc/0.8.20"

    class _Resolution:
        project_root = tmp_path / "resolved_root"
        solc = _Solc()

        def remapping_args(self):
            return ["ds-test/=/deps/ds-test/"]

    S.verify_with_prover(
        src,
        "rule r1 {}",
        output_dir=out,
        table=table,
        resolution=_Resolution(),
        certora_bin="/fake/certoraRun",
        runner=runner,
    )

    argv = captured["cmd"]
    assert argv[argv.index("--solc") + 1] == "/solc/0.8.20"
    assert "ds-test/=/deps/ds-test/" in argv
    assert captured["cwd"] == str(tmp_path / "resolved_root")


def test_directory_input_uses_contract_name_not_dir_stem(tmp_path):
    # Directory input previously produced a literal "project:" target.
    proj = tmp_path / "myproj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "Token.sol").write_text("pragma solidity ^0.8.0;\ncontract Token {}\n")
    out = tmp_path / "out"
    table = _FakeTable({"Token": _FakeContract()})
    runner, captured = _capturing_runner()

    S.verify_with_prover(
        proj,
        "rule r1 {}",
        output_dir=out,
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
    )

    argv = captured["cmd"]
    target = argv[argv.index("--verify") + 1]
    assert target.startswith("Token:")
    assert not target.startswith("project:")
    assert not target.startswith("myproj:")


# ---------------------------------------------------------------------------
# default target selection  (R8.9, R8.10)
# ---------------------------------------------------------------------------


def test_default_target_excludes_inherited_contracts(tmp_path):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    # Vault inherits Base; Base is inherited-from, so default target is Vault.
    table = _FakeTable(
        {
            "Base": _FakeContract(),
            "Vault": _FakeContract(inheritance=["Base"]),
        }
    )
    runner, captured = _capturing_runner()

    S.verify_with_prover(
        src,
        "rule r1 {}",
        output_dir=out,
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
    )

    argv = captured["cmd"]
    targets = [argv[i + 1].split(":", 1)[0] for i, a in enumerate(argv) if a == "--verify"]
    assert targets == ["Vault"]


def test_explicit_verify_contracts_used_as_target(tmp_path):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    table = _FakeTable({"A": _FakeContract(), "B": _FakeContract()})
    runner, captured = _capturing_runner()

    S.verify_with_prover(
        src,
        "rule r1 {}",
        output_dir=out,
        table=table,
        verify_contracts=["B"],
        certora_bin="/fake/certoraRun",
        runner=runner,
    )

    argv = captured["cmd"]
    targets = [argv[i + 1].split(":", 1)[0] for i, a in enumerate(argv) if a == "--verify"]
    assert targets == ["B"]


# ---------------------------------------------------------------------------
# (b) tool_unavailable: null pass_rate + searched locations  (R20.4)
# ---------------------------------------------------------------------------


def test_tool_unavailable_yields_null_rates_and_searched_paths(tmp_path, monkeypatch):
    src = _sol(tmp_path)
    table = _FakeTable({"Vault": _FakeContract()})

    # Force certoraRun ABSENT hermetically (R10.2). certoraRun is now installed
    # in the venv bin dir, which ``_find_certora_bin`` searches BEFORE PATH, so
    # ``certora_bin=None`` alone no longer resolves absent; monkeypatching the
    # discovery seam to return None makes the test pass whether or not the tool
    # is installed. ``monkeypatch`` auto-reverts, leaking no global state.
    monkeypatch.setattr(S, "_find_certora_bin", lambda: None)
    report = S.verify_with_prover(
        src,
        "rule r1 {}",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin=None,
    )

    assert report["status"] == "tool_unavailable"
    # Null, not zero (R20.4).
    assert report["pass_rate"] is None
    assert report["summary"]["pass_rate"] is None
    assert report["summary"]["passed"] is None
    assert report["summary"]["total_rules"] is None
    # Searched locations recorded (R20.4).
    assert report["searched_paths"]
    assert any("certoraRun" in p for p in report["searched_paths"])


def test_tool_unavailable_does_not_invoke_runner(tmp_path, monkeypatch):
    src = _sol(tmp_path)
    table = _FakeTable({"Vault": _FakeContract()})
    calls = []

    # Force certoraRun ABSENT hermetically (R10.2): the venv now ships it in the
    # interpreter bin dir, searched before PATH, so pin the discovery seam to
    # None so this asserts the tool-absent path regardless of what is installed.
    monkeypatch.setattr(S, "_find_certora_bin", lambda: None)

    def runner(*args, **kwargs):
        calls.append(args)
        raise AssertionError("runner must not be called when tool is absent")

    report = S.verify_with_prover(
        src, "rule r1 {}", output_dir=tmp_path / "out", table=table,
        certora_bin=None, runner=runner,
    )
    assert report["status"] == "tool_unavailable"
    assert calls == []


# ---------------------------------------------------------------------------
# (c) unknown --verify-contract errors without invoking certoraRun  (R8.13)
# ---------------------------------------------------------------------------


def test_unknown_verify_contract_errors_without_invoking_prover(tmp_path):
    src = _sol(tmp_path)
    table = _FakeTable({"Vault": _FakeContract(), "Helper": _FakeContract()})
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    with pytest.raises(S.VerifyContractError) as exc:
        S.verify_with_prover(
            src,
            "rule r1 {}",
            output_dir=tmp_path / "out",
            table=table,
            verify_contracts=["Nonexistent"],
            certora_bin="/fake/certoraRun",
            runner=runner,
        )

    msg = str(exc.value)
    assert "Nonexistent" in msg  # names the offending contract
    assert "Vault" in msg and "Helper" in msg  # names the available contracts
    assert calls == []  # certoraRun not invoked (R8.13)


# ---------------------------------------------------------------------------
# parse source recording: structured preferred  (R20.7)
# ---------------------------------------------------------------------------


def test_structured_results_preferred_and_recorded(tmp_path):
    src = _sol(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    # A machine-readable results file present in the output dir.
    (out / "verification_results.json").write_text(
        '{"rules": {"r1": "SUCCESS", "r2": "VIOLATED"}}'
    )
    table = _FakeTable({"Vault": _FakeContract()})

    def runner(cmd, capture_output=True, text=True, timeout=None, cwd=None):
        # Text output disagrees; structured must win.
        return subprocess.CompletedProcess(cmd, 0, "no rules here", "")

    report = S.verify_with_prover(
        src, "rule r1 {}", output_dir=out, table=table,
        certora_bin="/fake/certoraRun", runner=runner,
    )

    assert report["parse_source"] == "structured"  # R20.7
    names = {r["name"] for r in report["rules"]}
    assert names == {"r1", "r2"}
    verdicts = {r["name"]: r["status"] for r in report["rules"]}
    assert verdicts["r1"] == "PASSED"
    assert verdicts["r2"] == "FAILED"


# ---------------------------------------------------------------------------
# Verification_Report text printer/parser round-trip  (R20.9, task 6.7)
# ---------------------------------------------------------------------------


def _verdicts(report):
    return {r["name"]: r["status"] for r in report.get("rules") or []}


def test_mixed_report_round_trips(tmp_path):
    # A mixed report (passed + vacuous + failed) prints then parses back with
    # its status, rule-name set, per-rule verdict, and vacuous set preserved.
    rules = [
        {"name": "r_pass", "status": "PASSED"},
        {"name": "r_vac", "status": "VACUOUS"},
        {"name": "r_fail", "status": "FAILED"},
    ]
    status = S._classify_status(
        rules, tool_available=True, declared_rules=None, warnings=[]
    )
    report = {"status": status, "rules": rules, "warnings": []}

    text = S.render_report_text(report)
    parsed = S.parse_report_text(text)

    assert parsed["status"] == report["status"]  # a failing rule -> violated
    assert parsed["status"] == "violated"
    assert {r["name"] for r in parsed["rules"]} == {"r_pass", "r_vac", "r_fail"}
    assert _verdicts(parsed) == _verdicts(report)
    assert S.vacuous_rule_names(parsed) == S.vacuous_rule_names(report) == ["r_vac"]


def test_dead_verdict_round_trips_via_dead_line(tmp_path):
    # DEAD is not a token the Rule '<name>' STATUS pattern accepts, so the
    # printer aligns to the parser by emitting a dead-code line the parser
    # rewrites back to DEAD.
    rules = [{"name": "r_dead", "status": "DEAD"}]
    status = S._classify_status(
        rules, tool_available=True, declared_rules=None, warnings=[]
    )
    report = {"status": status, "rules": rules, "warnings": []}

    parsed = S.parse_report_text(S.render_report_text(report))

    assert _verdicts(parsed) == {"r_dead": "DEAD"}
    assert parsed["status"] == report["status"] == "vacuous"
    assert S.vacuous_rule_names(parsed) == ["r_dead"]


def test_zero_rule_report_round_trips(tmp_path):
    report = {"status": "not_run", "rules": [], "warnings": []}
    text = S.render_report_text(report)
    parsed = S.parse_report_text(text)
    assert parsed["status"] == "not_run"
    assert parsed["rules"] == []
    assert S.vacuous_rule_names(parsed) == []


def test_all_passing_report_round_trips_as_verified(tmp_path):
    rules = [{"name": "a", "status": "PASSED"}, {"name": "b", "status": "PASSED"}]
    status = S._classify_status(
        rules, tool_available=True, declared_rules=None, warnings=[]
    )
    report = {"status": status, "rules": rules, "warnings": []}
    parsed = S.parse_report_text(S.render_report_text(report))
    assert parsed["status"] == report["status"] == "verified"
    assert _verdicts(parsed) == {"a": "PASSED", "b": "PASSED"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
