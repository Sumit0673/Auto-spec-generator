"""Unit tests for the orchestrator's Stage 1 load-or-run path (task 4.1).

Covers the fix for the empty-table defect (Requirements 2.2, 4.2): the
orchestrator obtains the Stage 1 table from a fresh, non-stale Artifact_Store
envelope, or re-runs the extractor when the artifact is absent or stale - never
an empty fallback.

The unit under test is the pure helper
``spec_pipeline.pipeline._load_or_run_stage1``, which takes an injected extractor
callable ``(sol_path, output_dir) -> Stage1Table``. Injecting the extractor lets
these tests run WITHOUT slither: a real Stage 1 artifact is written to disk with
``artifacts.write_artifact`` + ``artifacts.serialize_stage1``, and the fake
extractor stands in for the slither-backed ``extract_first_party``.

slither is not installable here, so - as in ``test_artifacts_stage1_roundtrip``
- we register slither-free stubs for ``solidity_graph.analyzer`` and the
``spec_pipeline`` / ``solidity_graph`` parent packages BEFORE loading
``spec_pipeline.stage1_extract`` (whose module import eagerly pulls in the
slither-backed analyzer). ``pipeline.py`` itself imports the slither-backed
stages at module top, so we load only the pieces we need (``artifacts`` and the
two pipeline helpers) by file path via importlib rather than importing the whole
``spec_pipeline.pipeline`` module.
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
    """Load a module by file path, registering it before exec_module."""
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

CallerEdge = _stage1.CallerEdge
FirstPartyContract = _stage1.FirstPartyContract
FunctionGate = _stage1.FunctionGate
Stage1Table = _stage1.Stage1Table
StateVarInfo = _stage1.StateVarInfo


if "spec_pipeline.artifacts" not in sys.modules:
    artifacts = _load_module_by_path(
        "spec_pipeline.artifacts", "spec_pipeline/artifacts.py"
    )
else:  # pragma: no cover
    artifacts = sys.modules["spec_pipeline.artifacts"]


def _load_pipeline_helpers():
    """Load only the helper functions from pipeline.py without slither.

    ``pipeline.py`` imports the slither-backed stage modules at module top, so we
    cannot import it directly. We FORCE-install slither-free stubs for exactly
    the stage modules ``pipeline.py`` imports for the duration of the module
    exec - saving any pre-existing (real) module, overwriting it with the stub,
    then RESTORING the saved module afterward. Forcing (rather than skipping when
    already present) guarantees ``pipeline.py`` binds our stubs regardless of
    import order under pytest-randomly; restoring afterward leaves the real stage
    modules intact for other test files (e.g. ``test_stage5_verify`` imports the
    real ``spec_pipeline.stage5_verify``).

    The loaded pipeline module is registered under a UNIQUE private name so it
    never shadows the real ``spec_pipeline.pipeline`` for other test files. Tests
    monkeypatch stage callables on the returned module object directly.
    """
    stub_specs = {
        "spec_pipeline.stage2_invariants": ["mine_invariants"],
        "spec_pipeline.stage3_rules": ["write_rules"],
        "spec_pipeline.stage3_iterative": ["write_rules_iterative"],
        "spec_pipeline.stage4_critic": ["criticize", "apply_findings"],
        "spec_pipeline.stage5_verify": ["verify_with_prover"],
    }
    saved: dict[str, object] = {}
    for name, attrs in stub_specs.items():
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr in attrs:
            setattr(mod, attr, lambda *a, **k: None)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_stage1_load"
    try:
        if unique_name in sys.modules:  # pragma: no cover
            return sys.modules[unique_name]
        return _load_module_by_path(unique_name, "spec_pipeline/pipeline.py")
    finally:
        # Restore any real modules we shadowed so other test files are unaffected.
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


pipeline = _load_pipeline_helpers()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_table(project_path: str = "/proj") -> Stage1Table:
    table = Stage1Table(project_path=project_path)
    token = FirstPartyContract(name="Token", kind="contract", source_file="Token.sol")
    token.state_vars = [
        StateVarInfo("balance", "uint256", "public", False, False, ["mint"]),
    ]
    token.function_gates = [
        FunctionGate("mint", "(uint256 amt)", "external", "nonpayable", "onlyOwner"),
    ]
    token.caller_edges = [
        CallerEdge("Token", "mint", "Token", "_check", "internal"),
    ]
    table.contracts["Token"] = token
    return table


def _write_fresh_stage1_artifact(output_dir: Path, base: str, sol_path: Path,
                                 table: Stage1Table, fingerprint: str) -> None:
    """Write a fresh (current-version, current-fingerprint) Stage 1 envelope."""
    prov = artifacts.Provenance(
        pipeline_version=pipeline.PIPELINE_VERSION,
        stage=1,
        completed_utc="2024-01-01T00:00:00+00:00",
        source_path=str(sol_path),
        source_fingerprint=fingerprint,
        consumed=[],
    )
    artifacts.write_artifact(output_dir, base, 1, artifacts.serialize_stage1(table), prov)


class _RecordingExtractor:
    """Fake extractor recording whether it ran, returning a fixed table."""

    def __init__(self, table: Stage1Table):
        self._table = table
        self.calls = 0

    def __call__(self, sol_path: Path, output_dir: Path) -> Stage1Table:
        self.calls += 1
        return self._table


# ---------------------------------------------------------------------------
# Fresh artifact on disk -> deserialize the real (non-empty) table, no re-run
# (Requirements 2.2, 4.2)
# ---------------------------------------------------------------------------


def test_fresh_artifact_is_deserialized_not_reextracted(tmp_path):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}")
    base = artifacts.artifact_base_name(sol_path)
    fingerprint = pipeline._current_fingerprint(sol_path)

    on_disk = _sample_table()
    _write_fresh_stage1_artifact(tmp_path, base, sol_path, on_disk, fingerprint)

    # Extractor returns a DIFFERENT table; if it runs, we'd see that instead.
    extractor = _RecordingExtractor(Stage1Table(project_path="/should-not-be-used"))

    table, disposition = pipeline._load_or_run_stage1(
        tmp_path, base, sol_path, extractor, fingerprint
    )

    assert extractor.calls == 0
    assert disposition == pipeline.DISP_LOADED_CACHED
    # The real, non-empty table was reconstructed (this is the defect fix).
    assert list(table.contracts) == ["Token"]
    assert table.contracts["Token"].function_gates[0].name == "mint"
    assert table.to_text() == on_disk.to_text()


# ---------------------------------------------------------------------------
# Absent artifact -> run the injected extractor and persist the envelope
# ---------------------------------------------------------------------------


def test_absent_artifact_triggers_extraction_and_writes_envelope(tmp_path):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}")
    base = artifacts.artifact_base_name(sol_path)
    fingerprint = pipeline._current_fingerprint(sol_path)

    produced = _sample_table()
    extractor = _RecordingExtractor(produced)

    table, disposition = pipeline._load_or_run_stage1(
        tmp_path, base, sol_path, extractor, fingerprint
    )

    assert extractor.calls == 1
    assert disposition == pipeline.DISP_EXECUTED
    assert list(table.contracts) == ["Token"]

    # The envelope was written and now reloads as a fresh cache hit.
    reloaded = artifacts.load_artifact(tmp_path, base, 1)
    assert reloaded.present
    assert reloaded.contract_count == 1
    assert artifacts.is_stale(
        reloaded.provenance, fingerprint, pipeline.PIPELINE_VERSION, {}
    ) is None


# ---------------------------------------------------------------------------
# Stale artifact (fingerprint mismatch) -> re-extract (Requirement 3.3)
# ---------------------------------------------------------------------------


def test_stale_fingerprint_triggers_reextraction(tmp_path):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}")
    base = artifacts.artifact_base_name(sol_path)
    fingerprint = pipeline._current_fingerprint(sol_path)

    # Write an artifact whose recorded fingerprint does NOT match the current one.
    _write_fresh_stage1_artifact(
        tmp_path, base, sol_path, _sample_table(), "sha256:stale-does-not-match"
    )

    fresh = _sample_table(project_path="/reextracted")
    extractor = _RecordingExtractor(fresh)

    table, disposition = pipeline._load_or_run_stage1(
        tmp_path, base, sol_path, extractor, fingerprint
    )

    assert extractor.calls == 1
    assert disposition == pipeline.DISP_EXECUTED
    assert table.project_path == "/reextracted"

    # After re-extraction the envelope carries the CURRENT fingerprint.
    reloaded = artifacts.load_artifact(tmp_path, base, 1)
    assert reloaded.provenance.source_fingerprint == fingerprint


# ---------------------------------------------------------------------------
# Stale artifact (version mismatch) -> re-extract (Requirement 3.4)
# ---------------------------------------------------------------------------


def test_stale_version_triggers_reextraction(tmp_path):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}")
    base = artifacts.artifact_base_name(sol_path)
    fingerprint = pipeline._current_fingerprint(sol_path)

    prov = artifacts.Provenance(
        pipeline_version="0.0.0-ancient",
        stage=1,
        completed_utc="2024-01-01T00:00:00+00:00",
        source_path=str(sol_path),
        source_fingerprint=fingerprint,
        consumed=[],
    )
    artifacts.write_artifact(
        tmp_path, base, 1, artifacts.serialize_stage1(_sample_table()), prov
    )

    extractor = _RecordingExtractor(_sample_table(project_path="/reextracted"))
    table, disposition = pipeline._load_or_run_stage1(
        tmp_path, base, sol_path, extractor, fingerprint
    )

    assert extractor.calls == 1
    assert disposition == pipeline.DISP_EXECUTED


# ---------------------------------------------------------------------------
# Base name is consistent for file and directory inputs (Requirement 2.5)
# ---------------------------------------------------------------------------


def test_base_name_consistent_for_file_and_dir():
    assert artifacts.artifact_base_name(Path("/x/Pool.sol")) == "Pool"
    assert artifacts.artifact_base_name(Path("/x/aave-v3-core")) == "aave-v3-core"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
