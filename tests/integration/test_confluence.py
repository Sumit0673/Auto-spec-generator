"""Integration test for Property 15 - stage sequencing equivalence confluence.

**Validates: Requirements 4.1, 4.9**

One stages-1-through-5 invocation must produce the same stage artifacts,
terminal outcome, and exit code as five sequential single-stage invocations
against a fixed (here: fully deterministic) set of stage outputs. This is the
stale-cache defect encoded as an executable property: if a cached Stage 1 load
returned an empty table (the original defect) the two paths would diverge.

As in ``tests/unit/test_pipeline_zero_contract.py``, slither is not installable
in this environment, so we register slither-free stubs for
``solidity_graph.analyzer`` and the parent packages BEFORE loading
``spec_pipeline.stage1_extract``, then load ``spec_pipeline.pipeline`` by file
path via importlib with the LLM-backed stage modules stubbed. The stage
callables are then monkeypatched directly on the loaded pipeline module to
DETERMINISTIC outputs so both the one-shot and the five single-stage runs see
identical stage results - no slither, no LLM, no prover.
"""

from __future__ import annotations

import importlib.util
import json
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
FunctionGate = _stage1.FunctionGate
StateVarInfo = _stage1.StateVarInfo
Stage1Table = _stage1.Stage1Table


# LLM-backed stage modules replaced by inert stubs so pipeline.py imports
# offline. The concrete stage callables are monkeypatched per-test onto the
# loaded pipeline module object, so these stubs never actually run.
_STUB_STAGE_MODULES = {
    "spec_pipeline.stage2_invariants": {"mine_invariants": lambda *a, **k: {}},
    "spec_pipeline.stage3_rules": {"write_rules": lambda *a, **k: ""},
    "spec_pipeline.stage3_iterative": {"write_rules_iterative": lambda *a, **k: {}},
    "spec_pipeline.stage4_critic": {
        "criticize": lambda *a, **k: [],
        "apply_findings": lambda *a, **k: "",
    },
    "spec_pipeline.stage5_verify": {
        "verify_with_prover": lambda *a, **k: {"summary": {}}
    },
}


def _load_pipeline():
    """Load pipeline.py with LLM-backed stage modules replaced by inert stubs.

    We FORCE-install the stub stage modules for the duration of the pipeline
    module exec (saving any pre-existing real module, overwriting with the stub,
    restoring afterward) so pipeline.py binds without slither/LLM regardless of
    import order under pytest-randomly. The module is registered under a UNIQUE
    private name so it never shadows the real ``spec_pipeline.pipeline`` for
    other test files; this test monkeypatches stage callables on the returned
    module object directly.
    """
    saved: dict[str, object] = {}
    for name, attrs in _STUB_STAGE_MODULES.items():
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_confluence"
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


pipeline = _load_pipeline()


# ---------------------------------------------------------------------------
# Deterministic stage outputs (no slither, no LLM, no prover)
# ---------------------------------------------------------------------------

# A small, fixed Stage 1 table. Non-empty so the zero-contract short-circuit
# never fires and stages 2-5 all run.
def _fixed_table() -> Stage1Table:
    table = Stage1Table(project_path="/proj")
    contract = FirstPartyContract(
        name="Token", kind="contract", source_file="Token.sol"
    )
    contract.state_vars = [
        StateVarInfo(
            name="totalSupply",
            type="uint256",
            visibility="public",
            is_constant=False,
            is_immutable=False,
            writers=["mint"],
        )
    ]
    contract.function_gates = [
        FunctionGate(
            name="mint",
            signature="mint(address,uint256)",
            visibility="external",
            mutability="nonpayable",
            modifier="onlyOwner",
            is_constructor=False,
            is_fallback=False,
            is_receive=False,
        )
    ]
    table.contracts["Token"] = contract
    return table


_FIXED_INVARIANTS = {"Token": {"totalSupply": "monotonic"}}
_FIXED_CVL = "rule sanity { assert true; }"
_FIXED_FINDINGS: list = []  # empty -> no apply_findings rewrite; deterministic
_FIXED_REPORT = {"summary": {"status": "tool_unavailable", "rules": 1}}


def _patch_deterministic_stages(monkeypatch) -> None:
    """Monkeypatch every stage callable on the pipeline module to fixed outputs."""
    monkeypatch.setattr(pipeline, "extract_first_party", lambda *a, **k: _fixed_table())
    monkeypatch.setattr(pipeline, "mine_invariants", lambda *a, **k: dict(_FIXED_INVARIANTS))
    monkeypatch.setattr(pipeline, "write_rules", lambda *a, **k: _FIXED_CVL)
    monkeypatch.setattr(pipeline, "criticize", lambda *a, **k: list(_FIXED_FINDINGS))
    monkeypatch.setattr(pipeline, "apply_findings", lambda *a, **k: _FIXED_CVL)
    monkeypatch.setattr(
        pipeline, "verify_with_prover", lambda *a, **k: dict(_FIXED_REPORT)
    )


# ---------------------------------------------------------------------------
# Payload comparison
# ---------------------------------------------------------------------------
#
# We compare only the ``payload`` section of each stage envelope. The provenance
# fields that legitimately differ between two runs - completed_utc, duration,
# digest, run-id - all live in the separate ``provenance`` section, which this
# helper never reads, so ignoring them is structural rather than by field name.


def _artifact_payload(output_dir: Path, base: str, stage: int) -> dict:
    """Return the ``payload`` section of a stage artifact envelope."""
    path = output_dir / f"{base}_stage{stage}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert "payload" in envelope, f"{path} missing payload section"
    return envelope["payload"]


# ---------------------------------------------------------------------------
# Property 15: one 5-stage run == five single-stage runs
# ---------------------------------------------------------------------------


def test_confluence_five_stage_equals_five_single_stage(tmp_path, monkeypatch):
    _patch_deterministic_stages(monkeypatch)

    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    base = pipeline.artifact_base_name(sol_path)

    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"

    # Path A: one invocation running all five stages.
    results_a = pipeline.run_pipeline(sol_path, output_dir=out_a, stages=[1, 2, 3, 4, 5])

    # Path B: five sequential single-stage invocations into a separate dir. Each
    # returns its stage result; the orchestrator persists a canonical envelope
    # for stages 1-3, while stages 4-5 return their result directly. We compare
    # the persisted payloads (1-3) and the returned results (4-5) against the
    # one-shot run's stage artifact payloads.
    returns_b = {
        stage: pipeline.run_single_stage(stage, sol_path, output_dir=out_b)
        for stage in (1, 2, 3, 4, 5)
    }

    # Stages 1-3: persisted artifact PAYLOAD equal across the two paths. The
    # payload section carries none of the ignored provenance fields
    # (completed_utc / duration / digest / run-id live in the provenance
    # section, which we never read).
    for stage in (1, 2, 3):
        payload_a = _artifact_payload(out_a, base, stage)
        payload_b = _artifact_payload(out_b, base, stage)
        assert payload_a == payload_b, f"stage {stage} payload diverged"

    # Stage 4: path A persists ``{findings, cvl_spec}``; single-stage stage 4
    # returns the findings. The deterministic critic yields no findings, so the
    # spec is unchanged - assert both agree.
    payload4_a = _artifact_payload(out_a, base, 4)
    assert payload4_a["findings"] == returns_b[4]
    assert payload4_a["cvl_spec"] == _FIXED_CVL

    # Stage 5: path A persists the Verification_Report; single-stage stage 5
    # returns the same report object.
    payload5_a = _artifact_payload(out_a, base, 5)
    assert payload5_a == returns_b[5]

    # Same terminal outcome + exit code. A five-stage run over a non-empty table
    # completes with no short-circuit outcome set; the single-stage path raises
    # no PipelineOutcome (verified above by reaching here without exception).
    assert results_a["outcome"] is None
    assert results_a["exit_code"] is None


def test_confluence_stage1_payload_is_nonempty_table(tmp_path, monkeypatch):
    """Guard against the original defect: the reloaded Stage 1 table is non-empty.

    If the cached Stage 1 load returned an empty table (the stale-cache defect),
    the single-stage path B would carry zero contracts into stages 2-5 and the
    payloads would diverge. Assert the persisted Stage 1 payload declares the
    fixed contract so the confluence comparison is meaningful.
    """
    _patch_deterministic_stages(monkeypatch)

    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    base = pipeline.artifact_base_name(sol_path)
    out = tmp_path / "out"

    pipeline.run_single_stage(1, sol_path, output_dir=out)
    payload = _artifact_payload(out, base, 1)
    assert list(payload["contracts"]) == ["Token"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
