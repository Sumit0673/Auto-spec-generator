"""Integration test: a Stage 1-4 run completes via the LLM_Cache and reports
Stage 5 as ``tool_unavailable`` with certoraRun genuinely absent (task 8.6,
Requirement 5.7).

This is the acceptance test for R5.7: on a machine holding none of the external
tools, the Spec_Pipeline completes a Stage 1 through Stage 4 run against a
Fixture_Corpus project using the LLM_Cache and reports Stage 5 as
``tool_unavailable``. It is a fixture-backed integration test using a single
example (Requirement 23.11), not a property test.

Wiring, following the stub/importlib pattern of
``tests/unit/test_pipeline_zero_contract.py``:

* slither is not installable here, so slither-free stubs for
  ``solidity_graph.analyzer`` and the parent packages are registered BEFORE
  ``spec_pipeline.stage1_extract`` loads, and ``pipeline.py`` is loaded by file
  path with its LLM-backed stage modules replaced by stubs.
* ``pipeline.extract_first_party`` is monkeypatched to a small REAL
  ``Stage1Table`` holding one contract (no slither).
* ``mine_invariants`` / ``write_rules`` / ``criticize`` / ``apply_findings`` are
  monkeypatched to return recorded outputs. The Stage 3 CVL comes from the
  seeded LLM_Cache entry via the ``llm_cache_fixture`` (no network, no provider
  call).
* The REAL ``verify_with_prover`` runs for Stage 5. certoraRun is genuinely
  absent (the session guard strips it from PATH and it is installed nowhere),
  so Stage 5 records Verification_Status ``tool_unavailable`` with a null pass
  rate and the searched locations.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _REPO_ROOT / "tests" / "fixtures" / "corpus"

# The seeded LLM_Cache entry's prompt inputs (see the fixture JSON). The Stage 3
# stub replays this recorded response as the generated CVL spec.
_SEED_SYSTEM = "You are a Certora CVL spec generator."
_SEED_USER = "Generate a CVL rule for the Counter contract increment function."
_SEED_MODEL = "gpt-4o-mini"
_SEED_TEMPERATURE = 0.2


# ---------------------------------------------------------------------------
# Offline pipeline loading (same approach as test_pipeline_zero_contract.py)
# ---------------------------------------------------------------------------


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


# LLM-backed stage stubs installed so pipeline.py loads offline; the test
# monkeypatches the real behavior onto the returned module object.
_STUB_STAGES = {
    "spec_pipeline.stage2_invariants": {"mine_invariants": lambda *a, **k: {}},
    "spec_pipeline.stage3_rules": {"write_rules": lambda *a, **k: ""},
    "spec_pipeline.stage3_iterative": {"write_rules_iterative": lambda *a, **k: {}},
    "spec_pipeline.stage4_critic": {
        "criticize": lambda *a, **k: [],
        "apply_findings": lambda *a, **k: "",
    },
    # Stage 5 stub is a placeholder for import; the test restores the REAL one.
    "spec_pipeline.stage5_verify": None,
}


def _load_pipeline_offline():
    """Load pipeline.py with LLM-backed stages stubbed but the REAL Stage 5.

    Stages 2-4 modules are force-stubbed for the duration of the module exec so
    ``pipeline.py`` binds importable callables offline; Stage 5 is loaded for
    real (it is slither-free) so ``verify_with_prover`` genuinely runs. The
    stubs are restored afterward so other test files are unaffected. The module
    is registered under a unique private name so it never shadows the real
    ``spec_pipeline.pipeline``.
    """
    # Load the real Stage 5 module first so pipeline.py binds the genuine
    # verify_with_prover (certoraRun-absent path is what we are exercising).
    if "spec_pipeline.stage5_verify" not in sys.modules:
        _load_module_by_path(
            "spec_pipeline.stage5_verify", "spec_pipeline/stage5_verify.py"
        )

    saved: dict[str, object] = {}
    for name, attrs in _STUB_STAGES.items():
        if attrs is None:
            continue  # keep the real module (stage5_verify)
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_tool_absent"
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


pipeline = _load_pipeline_offline()


def _counter_table(project_path: str) -> Stage1Table:
    """A small REAL Stage 1 table with one first-party contract (Counter)."""
    table = Stage1Table(project_path=project_path)
    table.contracts["Counter"] = FirstPartyContract(
        name="Counter", kind="contract", source_file="Counter.sol"
    )
    return table


# ---------------------------------------------------------------------------
# The R5.7 acceptance test
# ---------------------------------------------------------------------------


def test_stage1_4_run_reports_stage5_tool_unavailable(
    tmp_path, monkeypatch, llm_cache_fixture
):
    """Stages 1-4 run from the LLM_Cache; Stage 5 is tool_unavailable (R5.7)."""
    source = _CORPUS / "single_file" / "Counter.sol"
    assert source.is_file(), "single_file corpus project must exist"
    out = tmp_path / "out"

    # certoraRun must be GENUINELY absent for this test: the session guard
    # strips it from PATH and it is installed nowhere. Assert that so a machine
    # that happens to have it does not silently invalidate the test.
    import shutil

    assert shutil.which("certoraRun") is None, (
        "certoraRun must be absent for the tool_unavailable acceptance test"
    )

    # Force certoraRun ABSENT hermetically (R10.2). certoraRun is now installed
    # in the venv bin dir, which Stage 5's ``_find_certora_bin`` searches BEFORE
    # PATH, so the ``shutil.which`` (PATH-only) check above no longer implies the
    # tool is undiscoverable. Pin the discovery seam on the REAL Stage 5 module
    # the pipeline imported to return None so the run behaves as on a tool-less
    # machine regardless of the venv installs. monkeypatch auto-reverts.
    _stage5 = sys.modules["spec_pipeline.stage5_verify"]
    monkeypatch.setattr(_stage5, "_find_certora_bin", lambda: None)

    # Stage 1: a small REAL table with one contract (no slither).
    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _counter_table(str(source))
    )

    # Stages 2-4: recorded outputs. The Stage 3 CVL comes from the seeded
    # LLM_Cache entry (no provider call, no network).
    recorded_cvl = llm_cache_fixture.get(
        _SEED_SYSTEM, _SEED_USER, _SEED_MODEL, _SEED_TEMPERATURE
    )
    stage2_recorded = {"Counter": [{"name": "count", "category": "monotonic"}]}
    monkeypatch.setattr(
        pipeline, "mine_invariants", lambda *a, **k: stage2_recorded
    )
    monkeypatch.setattr(pipeline, "write_rules", lambda *a, **k: recorded_cvl)
    monkeypatch.setattr(pipeline, "criticize", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "apply_findings", lambda *a, **k: recorded_cvl)

    # Stage 5 is the REAL verify_with_prover; certoraRun is absent.
    results = pipeline.run_pipeline(source, output_dir=out, stages=[1, 2, 3, 4, 5])

    # --- stages 1-4 dispositions: each executed (R4.6) ----------------------
    dispositions = results["run_manifest"]["stage_dispositions"]
    for stage in (1, 2, 3, 4, 5):
        assert dispositions[f"stage{stage}"] == pipeline.DISP_EXECUTED, (
            f"stage {stage} should have executed"
        )

    # The run did not short-circuit (a contract was present).
    assert results["outcome"] is None
    assert results["exit_code"] is None

    # --- stage 5 report: tool_unavailable, null pass rate, searched paths ---
    # results["stages"]["stage5"] carries the summary; the full report envelope
    # is written to {base}_stage5.json.
    base = results["artifact_base"]
    envelope_path = out / f"{base}_stage5.json"
    assert envelope_path.is_file(), "stage 5 artifact envelope must be written"
    report = json.loads(envelope_path.read_text())["payload"]

    assert report["status"] == "tool_unavailable"
    # pass_rate is None (NOT 0): an absent tool never reads as a measured zero.
    assert report["pass_rate"] is None
    assert results["stages"]["stage5"]["pass_rate"] is None
    # The searched locations for certoraRun are recorded (R20.4/R5.7).
    assert report["searched_paths"], "searched certoraRun locations must be recorded"
    assert any("certoraRun" in p for p in report["searched_paths"])
    # No numeric pass rate leaked into the per-verdict summary either.
    assert report["summary"]["passed"] is None
    assert report["summary"]["total_rules"] is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
