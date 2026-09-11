"""Unit tests for the zero-contract short-circuit + per-stage disposition
recording added in task 4.2.

Covers Requirements 4.3 (stop before any LLM call on a zero-contract Stage 1
table available to a stage numbered >=2; report ``no_first_party_contracts``
with exit 4), 4.6 (record per requested stage exactly one disposition, and for a
not-run stage the Outcome_Set member that stopped it), and 21.8 (record stage
durations and the digest of each written artifact).

As in ``test_pipeline_stage1_load.py``, slither is not installable here, so we
register slither-free stubs for ``solidity_graph.analyzer`` and the parent
packages BEFORE loading ``spec_pipeline.stage1_extract``, and we load only the
pipeline helpers by file path via importlib (``pipeline.py`` imports the
slither-backed stages at module top). The stubbed stage functions we install for
``pipeline.py`` are captured here as spies so a short-circuit test can assert no
LLM-backed stage was dispatched.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


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

if "spec_pipeline.artifacts" not in sys.modules:
    artifacts = _load_module_by_path(
        "spec_pipeline.artifacts", "spec_pipeline/artifacts.py"
    )
else:  # pragma: no cover
    artifacts = sys.modules["spec_pipeline.artifacts"]


class _Spy:
    """A callable that records its calls; stands in for an LLM-backed stage."""

    def __init__(self, name: str, ret):
        self.name = name
        self.ret = ret
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.ret


# LLM-backed stage spies. Installed as the stage modules ``pipeline.py`` imports
# so we can both load pipeline offline AND assert none of them ran.
SPIES = {
    "spec_pipeline.stage2_invariants": {"mine_invariants": _Spy("mine_invariants", {})},
    "spec_pipeline.stage3_rules": {"write_rules": _Spy("write_rules", "")},
    "spec_pipeline.stage3_iterative": {
        "write_rules_iterative": _Spy("write_rules_iterative", {})
    },
    "spec_pipeline.stage4_critic": {
        "criticize": _Spy("criticize", []),
        "apply_findings": _Spy("apply_findings", ""),
    },
    "spec_pipeline.stage5_verify": {
        "verify_with_prover": _Spy("verify_with_prover", {"summary": {}})
    },
}


def _load_pipeline_with_spies():
    """Load pipeline.py with LLM-backed stages replaced by recording spies.

    We FORCE-install the stub stage modules for the duration of the pipeline
    module exec - saving any pre-existing (real) module, overwriting it with the
    spy stub, then RESTORING the saved module afterward. Forcing (rather than
    skipping when already present) guarantees ``pipeline.py`` binds our spies
    regardless of import order under pytest-randomly; restoring afterward leaves
    the real stage modules intact for other test files. The loaded module is
    registered under a UNIQUE private name so it never shadows the real
    ``spec_pipeline.pipeline`` for other files; tests monkeypatch stage callables
    on the returned module object directly.
    """
    saved: dict[str, object] = {}
    for name, attrs in SPIES.items():
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, spy in attrs.items():
            setattr(mod, attr, spy)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_zero_contract"
    try:
        if unique_name in sys.modules:  # pragma: no cover
            return sys.modules[unique_name]
        return _load_module_by_path(unique_name, "spec_pipeline/pipeline.py")
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


pipeline = _load_pipeline_with_spies()


def _empty_table() -> Stage1Table:
    return Stage1Table(project_path="/proj")


def _nonempty_table() -> Stage1Table:
    table = Stage1Table(project_path="/proj")
    table.contracts["Token"] = FirstPartyContract(
        name="Token", kind="contract", source_file="Token.sol"
    )
    return table


# ---------------------------------------------------------------------------
# Pure decision helper: _zero_contract_outcome (slither-free)
# ---------------------------------------------------------------------------


def test_zero_contracts_with_stage_ge2_short_circuits():
    result = pipeline._zero_contract_outcome(0, [1, 2, 3, 4, 5])
    assert result == (pipeline.OUTCOME_NO_FIRST_PARTY_CONTRACTS,
                      pipeline.EXIT_NO_FIRST_PARTY_CONTRACTS)
    assert result[1] == 4


def test_zero_contracts_single_stage_ge2_short_circuits():
    assert pipeline._zero_contract_outcome(0, [5]) == (
        pipeline.OUTCOME_NO_FIRST_PARTY_CONTRACTS, 4
    )


def test_zero_contracts_only_stage1_does_not_short_circuit():
    # Stage 1 alone against zero contracts is a legitimate (empty) extraction.
    assert pipeline._zero_contract_outcome(0, [1]) is None


def test_nonzero_contracts_never_short_circuits():
    assert pipeline._zero_contract_outcome(1, [1, 2, 3, 4, 5]) is None
    assert pipeline._zero_contract_outcome(3, [2]) is None


# ---------------------------------------------------------------------------
# Pure recording helpers: durations, artifact digests, not-run outcomes
# ---------------------------------------------------------------------------


def test_record_stage_manifest_writes_disposition_and_detail():
    manifest = {"stage_dispositions": {}, "stage_details": {}}
    pipeline._record_stage_manifest(
        manifest, 2, pipeline.DISP_EXECUTED,
        duration_s=1.25, artifact_sha256="sha256:abc",
    )
    assert manifest["stage_dispositions"]["stage2"] == pipeline.DISP_EXECUTED
    detail = manifest["stage_details"]["stage2"]
    assert detail["disposition"] == pipeline.DISP_EXECUTED
    assert detail["duration_s"] == 1.25
    assert detail["artifact_sha256"] == "sha256:abc"
    assert "outcome" not in detail


def test_record_not_run_stages_marks_only_remaining_stages():
    manifest = {"stage_dispositions": {}, "stage_details": {}}
    # Stage 1 already has a disposition; it must not be overwritten.
    pipeline._record_stage_manifest(manifest, 1, pipeline.DISP_EXECUTED)
    pipeline._record_not_run_stages(
        manifest, [1, 2, 3, 4, 5], pipeline.OUTCOME_NO_FIRST_PARTY_CONTRACTS
    )
    assert manifest["stage_dispositions"]["stage1"] == pipeline.DISP_EXECUTED
    for stage in (2, 3, 4, 5):
        key = f"stage{stage}"
        assert manifest["stage_dispositions"][key] == pipeline.DISP_NOT_RUN
        assert (
            manifest["stage_details"][key]["outcome"]
            == pipeline.OUTCOME_NO_FIRST_PARTY_CONTRACTS
        )


def test_artifact_sha256_matches_written_bytes(tmp_path):
    base = "Token"
    prov = artifacts.Provenance(
        pipeline_version=pipeline.PIPELINE_VERSION, stage=1,
        completed_utc="2024-01-01T00:00:00+00:00", source_path="/x/Token.sol",
        source_fingerprint="sha256:deadbeef", consumed=[],
    )
    path = artifacts.write_artifact(
        tmp_path, base, 1, artifacts.serialize_stage1(_nonempty_table()), prov
    )
    import hashlib
    expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert pipeline._artifact_sha256(tmp_path, base, 1) == expected


def test_artifact_sha256_absent_returns_none(tmp_path):
    assert pipeline._artifact_sha256(tmp_path, "Missing", 3) is None


# ---------------------------------------------------------------------------
# run_pipeline: zero-contract short-circuit end to end (no LLM stage dispatched)
# ---------------------------------------------------------------------------


def test_run_pipeline_short_circuits_on_zero_contracts(tmp_path, monkeypatch):
    sol_path = tmp_path / "Empty.sol"
    sol_path.write_text("// no contract declarations\n")
    out = tmp_path / "out"

    # Stage 1 "extracts" an empty table; no slither involved.
    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _empty_table()
    )

    # Any LLM-backed stage dispatch is a failure: it must be stopped before.
    def _boom(*a, **k):  # pragma: no cover - only runs if the short-circuit fails
        raise AssertionError("an LLM-backed stage was dispatched before short-circuit")

    for attr in ("mine_invariants", "write_rules", "write_rules_iterative",
                 "criticize", "apply_findings", "verify_with_prover"):
        monkeypatch.setattr(pipeline, attr, _boom)

    results = pipeline.run_pipeline(sol_path, output_dir=out, stages=[1, 2, 3, 4, 5])

    # Outcome + exit code the CLI (task 7.1) maps to exit 4 (Requirement 4.3).
    assert results["outcome"] == pipeline.OUTCOME_NO_FIRST_PARTY_CONTRACTS
    assert results["exit_code"] == 4

    # (No LLM-backed stage ran: the _boom guards above would have raised.)

    # Stage 1 executed; remaining requested stages recorded not-run with outcome.
    manifest = results["run_manifest"]
    assert manifest["stage_dispositions"]["stage1"] == pipeline.DISP_EXECUTED
    for stage in (2, 3, 4, 5):
        key = f"stage{stage}"
        assert manifest["stage_dispositions"][key] == pipeline.DISP_NOT_RUN
        assert (
            manifest["stage_details"][key]["outcome"]
            == pipeline.OUTCOME_NO_FIRST_PARTY_CONTRACTS
        )

    # Stage 1 detail carries a duration and the written-artifact digest (R21.8).
    detail1 = manifest["stage_details"]["stage1"]
    assert detail1["duration_s"] is not None
    assert detail1["artifact_sha256"] == pipeline._artifact_sha256(out, "Empty", 1)


def test_run_pipeline_stage1_only_zero_contracts_no_short_circuit(tmp_path, monkeypatch):
    sol_path = tmp_path / "Empty.sol"
    sol_path.write_text("// no contract declarations\n")
    out = tmp_path / "out"

    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _empty_table()
    )
    # With stages=[1] the short-circuit does not fire, but the orchestrator still
    # loads/runs the downstream stages via their else-branches; give them safe
    # stubs so this test is independent of import-time stub binding.
    monkeypatch.setattr(pipeline, "mine_invariants", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "write_rules", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "criticize", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "apply_findings", lambda *a, **k: "")
    monkeypatch.setattr(
        pipeline, "verify_with_prover", lambda *a, **k: {"summary": {}}
    )

    # Only stage 1 requested: an empty extraction is legitimate, so the
    # zero-contract short-circuit does NOT fire (no outcome/exit set).
    results = pipeline.run_pipeline(sol_path, output_dir=out, stages=[1])
    assert results["outcome"] is None
    assert results["exit_code"] is None
    assert results["run_manifest"]["stage_dispositions"]["stage1"] == (
        pipeline.DISP_EXECUTED
    )


def test_run_pipeline_with_contracts_does_not_short_circuit(tmp_path, monkeypatch):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    out = tmp_path / "out"

    # Monkeypatch the stage callables directly on the pipeline module so this
    # test is independent of which stub bound them at import (the module object
    # is shared across test files via sys.modules).
    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _nonempty_table()
    )
    miner = _Spy("mine_invariants", {})
    writer = _Spy("write_rules", "")
    monkeypatch.setattr(pipeline, "mine_invariants", miner)
    monkeypatch.setattr(pipeline, "write_rules", writer)
    monkeypatch.setattr(pipeline, "criticize", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "apply_findings", lambda *a, **k: "")
    monkeypatch.setattr(
        pipeline, "verify_with_prover", lambda *a, **k: {"summary": {}}
    )

    # A table WITH contracts proceeds past the short-circuit into the stages.
    results = pipeline.run_pipeline(sol_path, output_dir=out, stages=[1, 2, 3, 4, 5])
    assert results["outcome"] is None
    assert results["exit_code"] is None
    # The invariant miner + rule writer ran (no short-circuit).
    assert miner.calls == 1
    assert writer.calls == 1


# ---------------------------------------------------------------------------
# run_single_stage: zero-contract short-circuit raises PipelineOutcome (exit 4)
# ---------------------------------------------------------------------------


def test_run_single_stage_ge2_zero_contracts_raises_outcome(tmp_path, monkeypatch):
    sol_path = tmp_path / "Empty.sol"
    sol_path.write_text("// no contract declarations\n")
    out = tmp_path / "out"

    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _empty_table()
    )

    def _boom(*a, **k):  # pragma: no cover - only runs if short-circuit fails
        raise AssertionError("a stage was dispatched before short-circuit")

    monkeypatch.setattr(pipeline, "mine_invariants", _boom)

    with pytest.raises(pipeline.PipelineOutcome) as excinfo:
        pipeline.run_single_stage(2, sol_path, output_dir=out)

    assert excinfo.value.outcome == pipeline.OUTCOME_NO_FIRST_PARTY_CONTRACTS
    assert excinfo.value.exit_code == 4


def test_run_single_stage1_zero_contracts_no_short_circuit(tmp_path, monkeypatch):
    sol_path = tmp_path / "Empty.sol"
    sol_path.write_text("// no contract declarations\n")
    out = tmp_path / "out"

    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _empty_table()
    )

    # Stage 1 by itself must not raise; it returns the (empty) table.
    table = pipeline.run_single_stage(1, sol_path, output_dir=out)
    assert list(table.contracts) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
