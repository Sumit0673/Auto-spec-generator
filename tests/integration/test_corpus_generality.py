"""Fixture-corpus generality integration test (task 13.4, Requirement 19.2).

Requirement 19.2: WHEN the Spec_Pipeline runs against any Fixture_Corpus project,
it SHALL terminate with EXACTLY ONE outcome from the Outcome_Set.

This is the executable form of "works for every type of contract" as total
*outcome* coverage rather than total success: for every one of the twelve
Contract_Shape_Taxonomy projects under ``tests/fixtures/corpus/`` the pipeline
drives to a single classified :class:`spec_pipeline.outcomes.Outcome` member -
never zero (an unhandled exception) and never two (an ambiguous terminal).

Offline constraints (Requirements 10.2, 23.11, 23.14). slither, solc, and
certoraRun are ALL absent (the session guards in ``tests/conftest.py`` strip the
binaries from PATH and block the network), so a real five-stage run cannot
compile. This test therefore reproduces, deterministically, the *offline*
terminal each shape reaches, matching ``corpus_manifest.json`` where a recording
or resolver decision exists:

* ``tool_unavailable`` shapes (single_file, multi_contract, inheritance_depth,
  library_dependent, struct_enum_signature, upgradeable_proxy, foundry_layout,
  hardhat_layout, npm_dependency): Stage 1 is monkeypatched to a small REAL
  ``Stage1Table`` (one contract), Stages 2-4 replay the seeded LLM_Cache /
  recorded outputs, and the REAL Stage 5 ``verify_with_prover`` runs. certoraRun
  is genuinely absent, so Stage 5 records Verification_Status
  ``tool_unavailable`` - the single terminal for these shapes here. (Reuses the
  LLM_Cache + real-Stage-5 pattern of ``test_tool_absent_run.py``, task 8.6.)
* ``no_first_party_contracts`` shapes (interface_only, abstract_contract): the
  Extractor finds zero first-party contracts, so Stage 1 is monkeypatched to an
  EMPTY ``Stage1Table`` and the zero-contract short-circuit (Requirement 4.3)
  reports ``no_first_party_contracts`` before any LLM call.
* ``unsupported_pragma_set`` shape (mixed_pragmas): two first-party files declare
  disjoint solc pragmas (0.6.12 vs 0.8.19). The Project_Resolver detects this
  from the sources alone - independent of any installed solc - and reports
  ``unsupported_pragma_set`` before any prover invocation (Requirement 8.7).

This is a fixture-backed integration test (one example per shape), not a
hypothesis property test (Requirement 23.11).

Pipeline loading follows the slither-free stub + importlib pattern used by the
sibling integration tests: register stubs for ``solidity_graph.analyzer`` and
the parent packages BEFORE ``spec_pipeline.stage1_extract`` loads, load the real
Stage 5 module (so ``verify_with_prover`` genuinely runs the certoraRun-absent
path), and load ``pipeline.py`` by file path under a unique private name with the
LLM-backed Stage 2-4 modules stubbed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _REPO_ROOT / "tests" / "fixtures" / "corpus"
_MANIFEST = _CORPUS / "corpus_manifest.json"

# The seeded LLM_Cache entry's prompt inputs (see the fixture JSON). Stages 2-4
# replay this recorded response as the generated CVL spec for the
# ``tool_unavailable`` shapes; no provider call, no network.
_SEED_SYSTEM = "You are a Certora CVL spec generator."
_SEED_USER = "Generate a CVL rule for the Counter contract increment function."
_SEED_MODEL = "gpt-4o-mini"
_SEED_TEMPERATURE = 0.2


# ---------------------------------------------------------------------------
# Offline pipeline loading (same approach as test_tool_absent_run.py)
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

# The Outcome_Set enum (imported directly; the module is pure/slither-free).
if "spec_pipeline.outcomes" not in sys.modules:
    _outcomes = _load_module_by_path(
        "spec_pipeline.outcomes", "spec_pipeline/outcomes.py"
    )
else:  # pragma: no cover - depends on collection order
    _outcomes = sys.modules["spec_pipeline.outcomes"]
Outcome = _outcomes.Outcome

# The Project_Resolver, for the unsupported_pragma_set shape (pure, no binaries).
if "spec_pipeline.resolve" not in sys.modules:
    _resolve = _load_module_by_path("spec_pipeline.resolve", "spec_pipeline/resolve.py")
else:  # pragma: no cover - depends on collection order
    _resolve = sys.modules["spec_pipeline.resolve"]
Project_Resolver = _resolve.Project_Resolver


# LLM-backed Stage 2-4 modules replaced by inert stubs so pipeline.py imports
# offline; Stage 5 is loaded REAL so the certoraRun-absent path genuinely runs.
_STUB_STAGES = {
    "spec_pipeline.stage2_invariants": {"mine_invariants": lambda *a, **k: {}},
    "spec_pipeline.stage3_rules": {"write_rules": lambda *a, **k: ""},
    "spec_pipeline.stage3_iterative": {"write_rules_iterative": lambda *a, **k: {}},
    "spec_pipeline.stage4_critic": {
        "criticize": lambda *a, **k: [],
        "apply_findings": lambda *a, **k: "",
    },
    "spec_pipeline.stage5_verify": None,  # keep the REAL module
}


def _load_pipeline_offline():
    """Load pipeline.py with Stage 2-4 stubbed and the REAL Stage 5 module."""
    if "spec_pipeline.stage5_verify" not in sys.modules:
        _load_module_by_path(
            "spec_pipeline.stage5_verify", "spec_pipeline/stage5_verify.py"
        )

    saved: dict[str, object] = {}
    for name, attrs in _STUB_STAGES.items():
        if attrs is None:
            continue
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_corpus_generality"
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


# ---------------------------------------------------------------------------
# Manifest + per-shape analyzed-path resolution
# ---------------------------------------------------------------------------


def _load_manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _analyzed_path(project_dir: Path) -> Path:
    """Return the path the pipeline analyzes for a corpus project.

    A single ``.sol`` file at the project root is analyzed as that file; a
    structured layout (foundry/hardhat/npm, or the two-file mixed_pragmas) is
    analyzed as its directory. This mirrors how an operator would invoke the CLI
    on each shape.
    """
    sols = sorted(p for p in project_dir.iterdir() if p.suffix == ".sol")
    if len(sols) == 1:
        return sols[0]
    return project_dir


def _one_contract_table(project_path: Path) -> Stage1Table:
    """A small REAL Stage 1 table with exactly one first-party contract."""
    table = Stage1Table(project_path=str(project_path))
    table.contracts["Sample"] = FirstPartyContract(
        name="Sample", kind="contract", source_file="Sample.sol"
    )
    return table


def _empty_table(project_path: Path) -> Stage1Table:
    """A REAL Stage 1 table with zero contracts (Extractor found none)."""
    return Stage1Table(project_path=str(project_path))


# ---------------------------------------------------------------------------
# Per-category drivers, each returning EXACTLY ONE Outcome member
# ---------------------------------------------------------------------------


def _drive_tool_unavailable(project_dir: Path, tmp_path, monkeypatch, llm_cache_fixture) -> Outcome:
    """Run stages 1-4 from the LLM_Cache; the REAL Stage 5 is tool_unavailable."""
    analyzed = _analyzed_path(project_dir)
    out = tmp_path / "out"

    # certoraRun must be genuinely absent for this terminal to be honest.
    assert shutil.which("certoraRun") is None, (
        "certoraRun must be absent for the tool_unavailable terminal"
    )

    # Force certoraRun ABSENT hermetically (R10.2). It is now installed in the
    # venv bin dir, which Stage 5's ``_find_certora_bin`` searches BEFORE PATH,
    # so the PATH-only ``shutil.which`` check above no longer implies the tool is
    # undiscoverable. Pin the discovery seam on the REAL Stage 5 module the
    # pipeline imported to None so this shape reaches the tool_unavailable
    # terminal regardless of the venv installs. monkeypatch auto-reverts.
    _stage5 = sys.modules["spec_pipeline.stage5_verify"]
    monkeypatch.setattr(_stage5, "_find_certora_bin", lambda: None)

    recorded_cvl = llm_cache_fixture.get(
        _SEED_SYSTEM, _SEED_USER, _SEED_MODEL, _SEED_TEMPERATURE
    )

    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _one_contract_table(analyzed)
    )
    monkeypatch.setattr(
        pipeline, "mine_invariants", lambda *a, **k: {"Sample": []}
    )
    monkeypatch.setattr(pipeline, "write_rules", lambda *a, **k: recorded_cvl)
    monkeypatch.setattr(pipeline, "criticize", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "apply_findings", lambda *a, **k: recorded_cvl)

    results = pipeline.run_pipeline(analyzed, output_dir=out, stages=[1, 2, 3, 4, 5])

    # No short-circuit fired (a contract was present): the terminal is Stage 5's
    # Verification_Status, and it must be exactly tool_unavailable.
    assert results["outcome"] is None
    assert results["exit_code"] is None
    base = results["artifact_base"]
    report = json.loads((out / f"{base}_stage5.json").read_text())["payload"]
    assert report["status"] == "tool_unavailable"
    assert report["pass_rate"] is None  # absent tool never reads as measured zero
    return Outcome(report["status"])


def _drive_no_first_party(project_dir: Path, tmp_path, monkeypatch) -> Outcome:
    """Zero first-party contracts short-circuits to no_first_party_contracts."""
    analyzed = _analyzed_path(project_dir)
    out = tmp_path / "out"

    monkeypatch.setattr(
        pipeline, "extract_first_party", lambda *a, **k: _empty_table(analyzed)
    )

    results = pipeline.run_pipeline(analyzed, output_dir=out, stages=[1, 2, 3, 4, 5])

    # The short-circuit sets the outcome and returns before any LLM stage.
    assert results["outcome"] == "no_first_party_contracts"
    assert results["exit_code"] == 4
    return Outcome(results["outcome"])


def _drive_unsupported_pragma_set(project_dir: Path) -> Outcome:
    """Disjoint first-party pragmas resolve to unsupported_pragma_set."""
    sources = sorted(p for p in project_dir.iterdir() if p.suffix == ".sol")
    assert len(sources) >= 2, "mixed_pragmas needs at least two first-party files"

    # No solc is installed (session guard); the disjoint-pragma decision is made
    # from the sources alone and is independent of any installed version.
    resolution = Project_Resolver().resolve(sources, installed_solc=[])
    assert resolution.solc is not None
    assert resolution.solc.outcome == "unsupported_pragma_set"
    # The conflicting files are named (Requirement 8.7).
    assert resolution.solc.conflicting_files
    return Outcome(resolution.solc.outcome)


# The single terminal each shape reaches OFFLINE, keyed by manifest expected_outcome.
_DRIVER_FOR_OUTCOME = {
    "tool_unavailable": "tool_unavailable",
    "no_first_party_contracts": "no_first_party",
    "unsupported_pragma_set": "unsupported_pragma_set",
}


def _terminal_outcome(
    shape: str, entry: dict, tmp_path, monkeypatch, llm_cache_fixture
) -> Outcome:
    """Drive one corpus shape to its single offline terminal Outcome member."""
    project_dir = _CORPUS / entry["project_dir"]
    assert project_dir.is_dir(), f"corpus project {shape} must exist"
    expected = entry["expected_outcome"]

    if expected == "tool_unavailable":
        return _drive_tool_unavailable(
            project_dir, tmp_path, monkeypatch, llm_cache_fixture
        )
    if expected == "no_first_party_contracts":
        return _drive_no_first_party(project_dir, tmp_path, monkeypatch)
    if expected == "unsupported_pragma_set":
        return _drive_unsupported_pragma_set(project_dir)
    raise AssertionError(  # pragma: no cover - guards a manifest change
        f"shape {shape!r} has unhandled expected_outcome {expected!r}; the offline "
        "terminal driver for this outcome must be added to task 13.4"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _shape_params():
    manifest = _load_manifest()
    return sorted(manifest["shapes"].items())


@pytest.mark.parametrize("shape,entry", _shape_params(), ids=lambda v: v if isinstance(v, str) else None)
def test_corpus_project_terminates_with_exactly_one_outcome(
    shape, entry, tmp_path, monkeypatch, llm_cache_fixture
):
    """Every Fixture_Corpus project terminates with EXACTLY ONE Outcome member.

    Requirement 19.2. The driver reaches a single terminal; we assert it is a
    valid Outcome_Set member (never zero: no unhandled exception; never two: a
    single classified terminal) and that it matches the manifest's recorded
    offline ``expected_outcome`` for that shape.
    """
    outcome = _terminal_outcome(shape, entry, tmp_path, monkeypatch, llm_cache_fixture)

    # Exactly one classified Outcome_Set member.
    assert isinstance(outcome, Outcome)
    # Consistent with the recorded offline terminal for this shape.
    assert outcome == Outcome(entry["expected_outcome"]), (
        f"shape {shape!r} terminated as {outcome.value!r}, "
        f"manifest expected {entry['expected_outcome']!r}"
    )


def test_corpus_manifest_covers_all_project_dirs():
    """Every corpus project directory is described by the manifest (and vice versa).

    Guards the generality claim: a new shape directory added to the corpus
    without a manifest entry (or an outcome with no offline driver) fails here
    rather than silently escaping the exactly-one-outcome assertion above.
    """
    manifest = _load_manifest()
    manifest_dirs = {e["project_dir"] for e in manifest["shapes"].values()}

    on_disk = {
        p.name
        for p in _CORPUS.iterdir()
        if p.is_dir() and p.name != "__pycache__"
    }
    assert on_disk == manifest_dirs, (
        "corpus project directories and manifest entries must match exactly"
    )

    # Every declared outcome has an offline driver (fails loudly on a new one).
    for entry in manifest["shapes"].values():
        assert entry["expected_outcome"] in _DRIVER_FOR_OUTCOME, (
            f"expected_outcome {entry['expected_outcome']!r} has no offline driver"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
