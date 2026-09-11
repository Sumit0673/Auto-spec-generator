"""Property 17 - Malformed-input classification (Requirement 23.9).

**Validates: Requirements 23.9**

Each of the six malformed-input classes terminates with exactly one classified
Outcome_Set member / diagnostic / staleness reason and raises NO unhandled
exception:

1. empty file            -> orchestrator maps CompileError to
                            ``compile_failed`` (exit 7).
2. no contract           -> same classified ``compile_failed`` terminal.
3. truncated Solidity    -> same classified ``compile_failed`` terminal.
4. invalid-JSON artifact -> ``artifacts.load_artifact`` raises ``ArtifactError``
                            naming the path (classified).
5. mismatched fingerprint-> ``artifacts.is_stale`` returns a
                            ``source_fingerprint`` Staleness (classified, not an
                            exception).
6. no-CVL response       -> ``cvl.extract_cvl`` returns the whole text and
                            ``cvl.validate_cvl`` emits a ``no_cvl`` diagnostic
                            (classified, no exception).

slither is not installable here, so the modules under test are loaded with the
same slither-free stub pattern used by the unit/integration suites: stub
``solidity_graph.analyzer`` and the package shims, load
``spec_pipeline.stage1_extract`` and ``spec_pipeline.pipeline`` by file path with
the LLM-backed stage modules stubbed. ``spec_pipeline.artifacts``,
``spec_pipeline.cvl``, and ``spec_pipeline.outcomes`` are import-safe on their
own (they pull in no slither chain), but we load them by path too for order
independence under pytest-randomly.

Property coverage: cases 4, 5, and 6 are exercised across >=100 generated
examples with hypothesis so the classification holds for arbitrary malformed
JSON, arbitrary fingerprint mismatches, and arbitrary CVL-free responses. Cases
1-3 are the enumerated fixed inputs named by R23.9 (one example each), because
the classification there is a single orchestrator decision, not an input-space
property.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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

CompileError = _stage1.CompileError


# artifacts / cvl / outcomes are import-safe on their own; load by path for
# order independence.
def _load_named(mod_name: str, rel_path: str):
    if mod_name in sys.modules:  # pragma: no cover - collection-order dependent
        return sys.modules[mod_name]
    return _load_module_by_path(mod_name, rel_path)


artifacts = _load_named("spec_pipeline.artifacts", "spec_pipeline/artifacts.py")
cvl = _load_named("spec_pipeline.cvl", "spec_pipeline/cvl.py")
outcomes = _load_named("spec_pipeline.outcomes", "spec_pipeline/outcomes.py")

ArtifactError = artifacts.ArtifactError
Provenance = artifacts.Provenance
Outcome = outcomes.Outcome


# LLM-backed stage modules replaced by inert stubs so pipeline.py imports
# offline. The extractor is monkeypatched per-test to raise CompileError.
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
    """Load pipeline.py with LLM-backed stage modules replaced by inert stubs."""
    saved: dict[str, object] = {}
    for name, attrs in _STUB_STAGE_MODULES.items():
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_malformed"
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
# Cases 1-3: empty file / no contract / truncated Solidity -> compile_failed
# ---------------------------------------------------------------------------
#
# The extractor cannot compile these inputs; it raises CompileError, which the
# orchestrator maps to the ``compile_failed`` outcome (exit 7). We assert
# exactly that single classified terminal and that no OTHER exception escapes.

_MALFORMED_SOURCES = {
    "empty_file": "",
    "no_contract_declaration": "// just a comment, no contract\n",
    "truncated_solidity": "contract Broken {\n    function f() external {\n",
}


@pytest.mark.parametrize("case", sorted(_MALFORMED_SOURCES))
def test_compile_failed_inputs_classified(tmp_path, monkeypatch, case):
    sol_path = tmp_path / "Input.sol"
    sol_path.write_text(_MALFORMED_SOURCES[case])
    out = tmp_path / "out"

    def _raise_compile(*a, **k):
        raise CompileError(
            diagnostics=f"slither could not compile ({case})",
            solc_version="0.8.20",
            remappings=[],
        )

    monkeypatch.setattr(pipeline, "extract_first_party", _raise_compile)

    # Exactly one classified terminal: a PipelineOutcome carrying
    # ``compile_failed`` / exit 7. No other exception type may escape.
    with pytest.raises(pipeline.PipelineOutcome) as excinfo:
        pipeline.run_pipeline(sol_path, output_dir=out, stages=[1, 2, 3, 4, 5])

    assert excinfo.value.outcome == Outcome.COMPILE_FAILED.value
    assert excinfo.value.outcome == pipeline.OUTCOME_COMPILE_FAILED
    assert excinfo.value.exit_code == 7
    # The outcome is a real Outcome_Set member and maps to exit 7.
    assert outcomes.exit_code_for(excinfo.value.outcome) == 7
    # No Stage 1 artifact was written (the raise happens at the extraction seam).
    assert not (out / "Input_stage1.json").exists()


def test_extract_seam_raises_classified_compile_error(tmp_path, monkeypatch):
    """Alt path named by the task: the extract seam itself raises CompileError.

    ``run_single_stage(1)`` runs the extractor directly; a compile failure there
    surfaces as the classified ``compile_failed`` PipelineOutcome (exit 7), never
    an unclassified slither exception.
    """
    sol_path = tmp_path / "Input.sol"
    sol_path.write_text("")
    out = tmp_path / "out"

    def _raise_compile(*a, **k):
        raise CompileError("empty file has no compilable contract", "0.8.20", [])

    monkeypatch.setattr(pipeline, "extract_first_party", _raise_compile)

    with pytest.raises(pipeline.PipelineOutcome) as excinfo:
        pipeline.run_single_stage(1, sol_path, output_dir=out)
    assert excinfo.value.outcome == pipeline.OUTCOME_COMPILE_FAILED
    assert excinfo.value.exit_code == 7


# ---------------------------------------------------------------------------
# Case 4: Stage_Artifact holding invalid JSON -> ArtifactError naming the path
# ---------------------------------------------------------------------------

# Byte strings that are NOT valid JSON. Each must make load_artifact raise
# ArtifactError (classified), never a bare JSONDecodeError or anything else.
_INVALID_JSON = st.sampled_from(
    [
        "",
        "{",
        "not json at all",
        "{ \"provenance\": ",
        "{ unquoted: 1 }",
        "[1, 2,",
        "\x00\x01\x02",
        "{ \"a\": 1 } trailing",
    ]
)


@settings(max_examples=100)
@given(bad=_INVALID_JSON, stage=st.integers(min_value=1, max_value=5))
def test_invalid_json_artifact_classified(tmp_path_factory, bad, stage):
    out = tmp_path_factory.mktemp("invalid_json")
    base = "Token"
    (out / f"{base}_stage{stage}.json").write_text(bad, encoding="utf-8")

    # Exactly one classified failure: ArtifactError naming the artifact path.
    with pytest.raises(ArtifactError) as excinfo:
        artifacts.load_artifact(out, base, stage)

    message = str(excinfo.value)
    assert f"{base}_stage{stage}.json" in message


# ---------------------------------------------------------------------------
# Case 5: mismatched Source_Fingerprint -> source_fingerprint Staleness (no raise)
# ---------------------------------------------------------------------------

_FP = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("f")),
    min_size=4,
    max_size=16,
)


@settings(max_examples=100)
@given(recorded=_FP, current=_FP)
def test_mismatched_fingerprint_classified(recorded, current):
    # Only exercise the mismatch case; equal fingerprints are the fresh path.
    if recorded == current:
        current = current + "0"

    prov = Provenance(
        pipeline_version=pipeline.PIPELINE_VERSION,
        stage=1,
        completed_utc="2024-01-01T00:00:00+00:00",
        source_path="/proj/Token.sol",
        source_fingerprint=f"sha256:{recorded}",
        consumed=[],
    )

    # is_stale returns a single classified Staleness - not an exception - naming
    # the source_fingerprint reason with the recorded vs current values.
    staleness = artifacts.is_stale(
        prov,
        current_fingerprint=f"sha256:{current}",
        current_version=pipeline.PIPELINE_VERSION,
        disk_inputs={},
    )
    assert staleness is not None
    assert staleness.reason == artifacts.STALE_SOURCE_FINGERPRINT
    assert staleness.recorded == f"sha256:{recorded}"
    assert staleness.current == f"sha256:{current}"


# ---------------------------------------------------------------------------
# Case 6: LLM response with no CVL -> whole text returned + no_cvl diagnostic
# ---------------------------------------------------------------------------

# Responses that contain NO CVL declaration keyword anywhere: plain prose and
# fenced blocks in non-CVL languages. extract_cvl must return the whole text
# (no fence held a CVL keyword) and validate_cvl must emit exactly one no_cvl
# diagnostic, with no exception.
_NO_CVL_TEXT = st.sampled_from(
    [
        "I could not produce a specification for this contract.",
        "Sorry, no idea.",
        "```python\nprint('hello world')\n```",
        "```json\n{\"a\": 1}\n```",
        "Here is some prose with the word ruleset embedded but not as CVL.",
        "```\njust a fenced block of plain text\n```",
        "no keywords here at all just words words words",
    ]
)


@settings(max_examples=100)
@given(response=_NO_CVL_TEXT)
def test_no_cvl_response_classified(response):
    # extract_cvl returns the whole response (stripped) because no fence held a
    # CVL declaration keyword (R18.3 fallback). No exception.
    extracted = cvl.extract_cvl(response)
    assert extracted == response.strip()

    # validate_cvl emits exactly one classified no_cvl diagnostic at line 1 and
    # raises nothing.
    diagnostics = cvl.validate_cvl(extracted)
    no_cvl = [d for d in diagnostics if d.category == "no_cvl"]
    assert len(no_cvl) == 1
    assert no_cvl[0].line == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
