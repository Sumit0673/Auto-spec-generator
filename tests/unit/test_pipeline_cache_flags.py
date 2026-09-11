"""Unit tests for the ``--no-cache`` / ``--require-cache`` cache-control flags
added in task 4.3.

Covers Requirements 3.7 (``--no-cache`` forces a MISS and leaves on-disk
artifacts unread, so every requested stage runs from its inputs and overwrites
its artifact), 3.8 (``--require-cache`` exits with code 3 naming the missing or
stale prerequisite artifact, running no stage and issuing no LLM call), and 1.6
(the flags default off, preserving pre-work-item behavior).

As in ``test_pipeline_zero_contract.py``, slither is not installable here, so we
register slither-free stubs for ``solidity_graph.analyzer`` and the parent
packages BEFORE loading ``spec_pipeline.stage1_extract``, and we load only the
pipeline helpers by file path via importlib (``pipeline.py`` imports the
slither-backed stages at module top). The LLM-backed stage functions
``pipeline.py`` imports are replaced with recording spies so a cache test can
assert exactly which stages ran (and, for ``--require-cache`` misses, that none
did).

The CLI-honors-``PipelineOutcome.exit_code`` check is an ast-level assertion on
``cli.py`` (like ``test_cli_outcome_wiring.py``): ``cli.py`` imports the pipeline
chain at module top and cannot be imported for real in this environment.
"""

from __future__ import annotations

import ast
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

    unique_name = "spec_pipeline._pipeline_under_test_cache_flags"
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


def _table(marker: str) -> Stage1Table:
    """A one-contract Stage 1 table whose source_file records *marker*.

    The marker lets a test tell a cache-loaded table apart from a freshly
    extracted one: the extractor returns a table marked ``"extracted"`` while a
    pre-written artifact carries a different marker.
    """
    table = Stage1Table(project_path="/proj")
    table.contracts["Token"] = FirstPartyContract(
        name="Token", kind="contract", source_file=f"Token.sol::{marker}"
    )
    return table


def _write_fresh_stage1(out: Path, base: str, sol_path: Path, marker: str) -> Path:
    """Write a fresh (matching-fingerprint) Stage 1 envelope carrying *marker*.

    Uses the pipeline's own writer + current-fingerprint helper so the artifact
    is genuinely fresh from ``is_stale``'s point of view.
    """
    fingerprint = pipeline._current_fingerprint(sol_path)
    pipeline._write_stage1_envelope(out, base, sol_path, _table(marker), fingerprint)
    return out / f"{base}_stage1.json"


def _write_stale_stage1(out: Path, base: str, sol_path: Path) -> Path:
    """Write a Stage 1 envelope with a mismatched fingerprint (stale by R3.3)."""
    prov = pipeline._make_provenance(
        1, sol_path, "sha256:stale-does-not-match", consumed=[]
    )
    return artifacts.write_artifact(
        out, base, 1, artifacts.serialize_stage1(_table("stale")), prov
    )


# ---------------------------------------------------------------------------
# R3.7 - --no-cache forces a MISS even when a fresh artifact exists
# ---------------------------------------------------------------------------


def test_no_cache_forces_miss_and_overwrites_fresh_artifact(tmp_path, monkeypatch):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    out = tmp_path / "out"
    out.mkdir()
    base = artifacts.artifact_base_name(sol_path)

    # A genuinely fresh Stage 1 artifact already exists on disk.
    artifact_path = _write_fresh_stage1(out, base, sol_path, marker="cached")

    # The injected extractor returns a DIFFERENT (marked) table and records runs.
    extractor = _Spy("extract_first_party", _table("extracted"))
    monkeypatch.setattr(pipeline, "extract_first_party", extractor)
    # Stage 2 spy so the requested single stage returns cleanly.
    miner = _Spy("mine_invariants", {})
    monkeypatch.setattr(pipeline, "mine_invariants", miner)

    # Run stage 2 with --no-cache: the fresh Stage 1 artifact must be ignored,
    # the extractor re-run, and the artifact overwritten (R3.7).
    pipeline.run_single_stage(2, sol_path, output_dir=out, no_cache=True)

    assert extractor.calls == 1, "no_cache must force the extractor to run"
    # The on-disk artifact was overwritten with the freshly extracted table.
    reloaded = artifacts.load_artifact(out, base, 1)
    assert reloaded.present
    assert (
        reloaded.payload["contracts"]["Token"]["source_file"]
        == "Token.sol::extracted"
    ), "the on-disk artifact must be overwritten by the extractor output"
    assert artifact_path.exists()


def test_default_reads_fresh_cache_without_extracting(tmp_path, monkeypatch):
    """R1.6: neither flag -> the fresh Stage 1 artifact is used, extractor idle."""
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    out = tmp_path / "out"
    out.mkdir()
    base = artifacts.artifact_base_name(sol_path)

    _write_fresh_stage1(out, base, sol_path, marker="cached")

    extractor = _Spy("extract_first_party", _table("extracted"))
    monkeypatch.setattr(pipeline, "extract_first_party", extractor)
    monkeypatch.setattr(pipeline, "mine_invariants", _Spy("mine_invariants", {}))

    # No flags: the fresh artifact is loaded and the extractor never runs.
    pipeline.run_single_stage(2, sol_path, output_dir=out)

    assert extractor.calls == 0, "default must reuse the fresh cached artifact"
    # The cached table (marker 'cached') is untouched on disk.
    reloaded = artifacts.load_artifact(out, base, 1)
    assert (
        reloaded.payload["contracts"]["Token"]["source_file"] == "Token.sol::cached"
    )


# ---------------------------------------------------------------------------
# R3.8 - --require-cache: absent / stale prerequisite -> exit 3 naming artifact
# ---------------------------------------------------------------------------


def test_require_cache_absent_prerequisite_raises_exit3(tmp_path, monkeypatch):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    out = tmp_path / "out"
    out.mkdir()
    base = artifacts.artifact_base_name(sol_path)
    artifact_path = out / f"{base}_stage1.json"

    # No Stage 1 artifact on disk. The extractor must NEVER run under
    # --require-cache (no stage runs, no LLM call).
    def _boom(*a, **k):  # pragma: no cover - only runs if the guard fails
        raise AssertionError("extractor ran under --require-cache with absent cache")

    monkeypatch.setattr(pipeline, "extract_first_party", _boom)
    monkeypatch.setattr(pipeline, "mine_invariants", _boom)

    with pytest.raises(pipeline.PipelineOutcome) as excinfo:
        pipeline.run_single_stage(2, sol_path, output_dir=out, require_cache=True)

    po = excinfo.value
    assert po.exit_code == 3
    assert po.outcome == pipeline.OUTCOME_REQUIRE_CACHE_MISS
    msg = str(po)
    assert str(artifact_path) in msg, "message must name the missing artifact"
    assert "absent" in msg


def test_require_cache_stale_prerequisite_raises_exit3_with_reason(
    tmp_path, monkeypatch
):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    out = tmp_path / "out"
    out.mkdir()
    base = artifacts.artifact_base_name(sol_path)

    # A STALE Stage 1 artifact (mismatched fingerprint) is on disk.
    artifact_path = _write_stale_stage1(out, base, sol_path)

    def _boom(*a, **k):  # pragma: no cover - only runs if the guard fails
        raise AssertionError("a stage ran under --require-cache with stale cache")

    monkeypatch.setattr(pipeline, "extract_first_party", _boom)
    monkeypatch.setattr(pipeline, "mine_invariants", _boom)

    with pytest.raises(pipeline.PipelineOutcome) as excinfo:
        pipeline.run_single_stage(2, sol_path, output_dir=out, require_cache=True)

    po = excinfo.value
    assert po.exit_code == 3
    assert po.outcome == pipeline.OUTCOME_REQUIRE_CACHE_MISS
    msg = str(po)
    assert str(artifact_path) in msg, "message must name the stale artifact"
    assert "stale" in msg
    # Names the staleness reason (fingerprint mismatch -> source_fingerprint).
    assert artifacts.STALE_SOURCE_FINGERPRINT in msg


def test_require_cache_fresh_prerequisite_proceeds(tmp_path, monkeypatch):
    sol_path = tmp_path / "Token.sol"
    sol_path.write_text("contract Token {}\n")
    out = tmp_path / "out"
    out.mkdir()
    base = artifacts.artifact_base_name(sol_path)

    # A FRESH Stage 1 artifact is present: --require-cache must proceed silently.
    _write_fresh_stage1(out, base, sol_path, marker="cached")

    # The extractor must not run (the fresh cache satisfies the prerequisite).
    extractor = _Spy("extract_first_party", _table("extracted"))
    monkeypatch.setattr(pipeline, "extract_first_party", extractor)
    miner = _Spy("mine_invariants", {})
    monkeypatch.setattr(pipeline, "mine_invariants", miner)

    result = pipeline.run_single_stage(
        2, sol_path, output_dir=out, require_cache=True
    )

    # No raise: stage 2 ran off the fresh cached Stage 1 table.
    assert extractor.calls == 0
    assert miner.calls == 1
    assert result == {}


# ---------------------------------------------------------------------------
# CLI honors PipelineOutcome.exit_code directly (ast-level, like
# test_cli_outcome_wiring.py). Exit 3 for a require-cache miss must come from
# ``po.exit_code``, not a remapped Outcome_Set lookup.
# ---------------------------------------------------------------------------

_CLI_PATH = _REPO_ROOT / "spec_pipeline" / "cli.py"
_CLI_TREE = ast.parse(_CLI_PATH.read_text())


def _pipeline_outcome_handler() -> ast.ExceptHandler:
    """Return the ``except PipelineOutcome`` handler node from cli.py."""
    for node in ast.walk(_CLI_TREE):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            names: list[str] = []
            if isinstance(node.type, ast.Name):
                names = [node.type.id]
            elif isinstance(node.type, ast.Tuple):
                names = [e.id for e in node.type.elts if isinstance(e, ast.Name)]
            if "PipelineOutcome" in names:
                return node
    raise AssertionError("cli.py has no `except PipelineOutcome` handler")


def test_cli_defines_no_cache_and_require_cache_flags():
    """cli.py registers the --no-cache and --require-cache argparse flags."""
    literals = {
        node.value
        for node in ast.walk(_CLI_TREE)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "--no-cache" in literals
    assert "--require-cache" in literals


def test_cli_threads_cache_flags_into_run_calls():
    """run_pipeline / run_single_stage are called with no_cache/require_cache."""
    threaded: dict[str, set[str]] = {}
    for node in ast.walk(_CLI_TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in ("run_pipeline", "run_single_stage"):
            threaded[name] = {kw.arg for kw in node.keywords if kw.arg}
    assert "run_pipeline" in threaded and "run_single_stage" in threaded
    for name, kwargs in threaded.items():
        assert "no_cache" in kwargs, f"{name} missing no_cache"
        assert "require_cache" in kwargs, f"{name} missing require_cache"


def test_cli_honors_pipeline_outcome_exit_code_directly():
    """The `except PipelineOutcome` branch passes ``po.exit_code`` through.

    Exit 3 (the --require-cache violation) is deliberately NOT an Outcome_Set
    exit code, so the handler must honor the raised ``po.exit_code`` directly
    (threaded into ``_finish``/``sys.exit``) rather than remapping the outcome
    through the shared table. We assert the handler body references
    ``<name>.exit_code`` where ``<name>`` is the bound exception.
    """
    handler = _pipeline_outcome_handler()
    bound = handler.name  # e.g. "po"
    assert bound, "the PipelineOutcome handler must bind the exception (as ...)"

    referenced_exit_code = False
    for node in ast.walk(handler):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "exit_code"
            and isinstance(node.value, ast.Name)
            and node.value.id == bound
        ):
            referenced_exit_code = True
    assert referenced_exit_code, (
        "the except PipelineOutcome branch must use po.exit_code directly, "
        "not a remapped/hardcoded exit code"
    )

    # And the outcome's exit code must NOT be a plain Outcome_Set table lookup
    # only: exit 3 is not in the table, so a bare exit_code_for(po.outcome) would
    # be wrong. Guard that the handler does not compute its exit solely from
    # exit_code_for without also passing po.exit_code (covered above).


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
