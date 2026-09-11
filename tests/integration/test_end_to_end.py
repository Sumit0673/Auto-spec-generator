"""End-to-end integration test for the five-stage Spec_Pipeline (task 8.5).

Requirement 10.5: the Test_Suite includes ONE end-to-end test that runs all five
stages against a Fixture_Corpus project using RECORDED LLM and prover output, and
asserts the resulting Spec_Bundle contents. No external tools (slither, solc,
certoraRun) are invoked and no network access occurs: Stage 1 is monkeypatched to
return a small real ``Stage1Table``, Stages 2-4 return recorded outputs, and Stage
5 returns a recorded Verification_Report whose Verification_Status is ``verified``.

This reuses the slither-free stub + importlib load pattern from
``tests/unit/test_pipeline_zero_contract.py``: ``pipeline.py`` imports the
slither-backed stage modules at module top, so we register slither-free stubs for
``solidity_graph.analyzer`` and the ``spec_pipeline`` package BEFORE loading the
pipeline, load ``pipeline.py`` by file path under a unique private name, and then
monkeypatch the stage callables directly on the loaded module.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The Fixture_Corpus project this end-to-end run analyzes (Requirement 10.5).
_CORPUS_SINGLE_FILE = _REPO_ROOT / "tests" / "fixtures" / "corpus" / "single_file"

# Recorded outputs (no real tools/network). The recorded Verification_Status the
# end-to-end run must carry through to the Spec_Bundle.
_RECORDED_STATUS = "verified"
_RECORDED_RULE = {"name": "incrementIncreasesCount", "status": "PASSED"}
_RECORDED_CVL_SPEC = (
    "// recorded CVL spec\n"
    "methods {\n"
    "    function count() external returns (uint256) envfree;\n"
    "}\n\n"
    "rule incrementIncreasesCount() {\n"
    "    env e;\n"
    "    uint256 before = count();\n"
    "    increment(e);\n"
    "    assert count() == before + 1;\n"
    "}\n"
)


def _install_stub_packages() -> None:
    """Register slither-free stubs so ``spec_pipeline.stage1_extract`` loads offline."""
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
else:  # pragma: no cover - depends on collection order
    _stage1 = sys.modules["spec_pipeline.stage1_extract"]

FirstPartyContract = _stage1.FirstPartyContract
Stage1Table = _stage1.Stage1Table


# LLM-backed stage stubs installed as the modules ``pipeline.py`` imports so the
# pipeline loads offline. The end-to-end test monkeypatches the callables on the
# loaded pipeline module directly, so these need only be present, not correct.
_STAGE_STUBS = {
    "spec_pipeline.stage2_invariants": {"mine_invariants": lambda *a, **k: {}},
    "spec_pipeline.stage3_rules": {"write_rules": lambda *a, **k: ""},
    "spec_pipeline.stage3_iterative": {
        "write_rules_iterative": lambda *a, **k: {}
    },
    "spec_pipeline.stage4_critic": {
        "criticize": lambda *a, **k: [],
        "apply_findings": lambda *a, **k: "",
    },
    "spec_pipeline.stage5_verify": {
        "verify_with_prover": lambda *a, **k: {"summary": {}}
    },
}


def _load_pipeline_offline():
    """Load ``pipeline.py`` with slither-free stage stubs, under a unique name."""
    saved: dict[str, object] = {}
    for name, attrs in _STAGE_STUBS.items():
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_end_to_end"
    try:
        if unique_name in sys.modules:  # pragma: no cover - collection order
            return sys.modules[unique_name]
        return _load_module_by_path(unique_name, "spec_pipeline/pipeline.py")
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


pipeline = _load_pipeline_offline()


def _recorded_stage1_table(project_path: Path) -> Stage1Table:
    """A small REAL Stage 1 table with exactly one contract (Counter)."""
    table = Stage1Table(project_path=str(project_path))
    table.contracts["Counter"] = FirstPartyContract(
        name="Counter", kind="contract", source_file="Counter.sol"
    )
    return table


def _recorded_verification_report() -> dict:
    """A recorded Verification_Report carrying exactly one PASSED rule.

    Mirrors the shape ``stage5_verify.verify_with_prover`` returns: ``status``
    (the Verification_Status), ``rules`` (per-rule verdicts), and ``summary``.
    """
    return {
        "status": _RECORDED_STATUS,
        "parse_source": "recorded",
        "rules": [dict(_RECORDED_RULE)],
        "warnings": [],
        "summary": {
            "status": _RECORDED_STATUS,
            "total_rules": 1,
            "passed": 1,
            "vacuous": 0,
            "failed": 0,
            "dead": 0,
            "timeout": 0,
            "pass_rate": 1.0,
        },
        "pass_rate": 1.0,
    }


def test_end_to_end_five_stages_recorded_no_real_tools(tmp_path, monkeypatch):
    """Run stages 1-5 over the single_file corpus with recorded LLM + prover output.

    Requirement 10.5: assert the resulting Spec_Bundle contents - a stage
    artifact (stage3/stage5) is written, the Verification_Report carries exactly
    one Verification_Status equal to the recorded status, the Run_Manifest records
    a disposition for each of stages 1-5, and the summary JSON is written.
    """
    out = tmp_path / "out"

    # Stage 1: recorded small real table (1 contract); no slither.
    monkeypatch.setattr(
        pipeline,
        "extract_first_party",
        lambda *a, **k: _recorded_stage1_table(_CORPUS_SINGLE_FILE),
    )
    # Stages 2-4: recorded outputs (no LLM).
    monkeypatch.setattr(
        pipeline, "mine_invariants", lambda *a, **k: {"Counter": []}
    )
    monkeypatch.setattr(
        pipeline, "write_rules", lambda *a, **k: _RECORDED_CVL_SPEC
    )
    monkeypatch.setattr(pipeline, "criticize", lambda *a, **k: [])
    monkeypatch.setattr(
        pipeline, "apply_findings", lambda *a, **k: _RECORDED_CVL_SPEC
    )
    # Stage 5: recorded Verification_Report (no certoraRun).
    report = _recorded_verification_report()
    monkeypatch.setattr(pipeline, "verify_with_prover", lambda *a, **k: report)

    single_file = _CORPUS_SINGLE_FILE / "Counter.sol"
    results = pipeline.run_pipeline(
        single_file, output_dir=out, stages=[1, 2, 3, 4, 5]
    )

    base = results["artifact_base"]

    # --- Spec_Bundle: a stage3 (CVL spec) and stage5 artifact are written ---
    stage3_artifact = out / f"{base}_stage3.json"
    stage5_artifact = out / f"{base}_stage5.json"
    assert stage3_artifact.exists(), "Stage 3 CVL-spec artifact must be written"
    assert stage5_artifact.exists(), "Stage 5 Verification_Report artifact written"

    stage3_payload = json.loads(stage3_artifact.read_text())["payload"]
    assert stage3_payload["cvl_spec"] == _RECORDED_CVL_SPEC

    # --- Verification_Report carries exactly one Verification_Status == recorded ---
    stage5_payload = json.loads(stage5_artifact.read_text())["payload"]
    assert stage5_payload["status"] == _RECORDED_STATUS
    assert results["stages"]["stage5"]["status"] == _RECORDED_STATUS
    assert len(stage5_payload["rules"]) == 1
    assert stage5_payload["rules"][0]["status"] == "PASSED"

    # --- Run_Manifest: one disposition per stage 1-5 ---
    dispositions = results["run_manifest"]["stage_dispositions"]
    for stage in (1, 2, 3, 4, 5):
        assert dispositions[f"stage{stage}"] == pipeline.DISP_EXECUTED

    # --- Summary JSON is written ---
    summary_file = out / f"{base}_pipeline_summary.json"
    assert summary_file.exists(), "pipeline summary JSON must be written"
    summary = json.loads(summary_file.read_text())
    assert summary["stages"]["stage5"]["status"] == _RECORDED_STATUS
    assert summary["run_manifest"]["stage_dispositions"] == dispositions


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
