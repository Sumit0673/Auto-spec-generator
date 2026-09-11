"""Property-based test for Property 15 - stage sequencing equivalence / confluence.

**Validates: Requirements 4.1, 4.9**

For a FIXED (deterministic) set of stage outputs, ONE stages-1-through-5
invocation of the pipeline must produce the SAME persisted stage-artifact
payloads (stages 1-3), the same stage-4 / stage-5 results, the same terminal
outcome, and the same exit code as FIVE sequential single-stage invocations.
This is the stale-cache defect encoded as an executable property: if a cached
Stage 1 load returned an empty table (the original defect), the two paths would
diverge.

This module is the ``@given`` property companion to the 2-example integration
test ``tests/integration/test_confluence.py``. It reuses that test's exact
offline machinery (``_install_stub_packages`` slither-free stubs,
``_load_pipeline`` force-installing inert stage-module stubs and loading
``pipeline.py`` by importlib under a UNIQUE private name) and generalises the
fixed bundle into a hypothesis ``@composite`` strategy that produces a
VARIED-but-deterministic stage-output bundle, asserting confluence across a
range of generated bundles rather than a single fixed one.

Offline design
--------------
slither / solc / certoraRun / the LLM endpoint are all unavailable (the session
conftest strips ``solc``/``certoraRun`` from PATH and blocks the network). We:

1. install slither-free stubs for ``solidity_graph.analyzer`` and the parent
   ``solidity_graph`` / ``spec_pipeline`` packages BEFORE loading anything,
2. load ``spec_pipeline.stage1_extract`` by file path via importlib to obtain
   the real ``FirstPartyContract`` / ``FunctionGate`` / ``StateVarInfo`` /
   ``Stage1Table`` types, and
3. load ``spec_pipeline.pipeline`` under a UNIQUE private module name with the
   LLM-backed stage modules replaced by inert stubs, so the module never shadows
   the real ``spec_pipeline.pipeline`` for other test files and collection order
   under pytest-randomly cannot break it.

The stage callables are patched directly on the loaded pipeline module to
DETERMINISTIC generated outputs (each callable returns fresh copies so path A
and path B observe identical values). No function-scoped fixture is used: this
is a pure ``@given`` test that save/restores the patched pipeline attributes in
a ``try/finally`` and creates its own ``tempfile.mkdtemp()`` output dirs, so the
``HealthCheck.function_scoped_fixture`` warning never applies.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Offline stubs + module load (mirrors tests/integration/test_confluence.py)
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
FunctionGate = _stage1.FunctionGate
StateVarInfo = _stage1.StateVarInfo
Stage1Table = _stage1.Stage1Table


# LLM-backed stage modules replaced by inert stubs so pipeline.py imports
# offline. The concrete stage callables are patched per-example onto the loaded
# pipeline module object, so these stubs never actually run.
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
    other test files; this test patches stage callables on the returned module
    object directly.
    """
    saved: dict[str, object] = {}
    for name, attrs in _STUB_STAGE_MODULES.items():
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod

    unique_name = "spec_pipeline._pipeline_under_test_confluence_property"
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
# Hypothesis strategies: a VARIED-but-deterministic stage-output bundle.
# ---------------------------------------------------------------------------
#
# A ``StageBundle`` holds the fixed outputs each patched stage callable returns.
# Every field is a plain, JSON-serialisable value (or a Stage1Table built from
# the real dataclasses) so the persisted envelope payloads are comparable across
# the two paths. Generators return the *building blocks*; the patched callables
# return fresh deep copies per call so path A and path B never share mutable
# state.

# Identifier-ish tokens for names/modifiers. Kept to a small readable alphabet
# so shrinking produces legible counterexamples; solidity identifiers cannot
# start with a digit, so we prefix a letter.
_IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,11}", fullmatch=True)
_MUTABILITY = st.sampled_from(["view", "pure", "payable", "nonpayable"])
_VISIBILITY = st.sampled_from(["public", "external"])
_VAR_VISIBILITY = st.sampled_from(["public", "internal", "private"])
_MODIFIER = st.sampled_from(["none", "onlyOwner", "onlyAdmin", "whenNotPaused"])


@st.composite
def _state_var(draw) -> StateVarInfo:
    """Generate a StateVarInfo with 0..3 writer function names."""
    return StateVarInfo(
        name=draw(_IDENT),
        type=draw(st.sampled_from(["uint256", "address", "bool", "bytes32", "int8"])),
        visibility=draw(_VAR_VISIBILITY),
        is_constant=draw(st.booleans()),
        is_immutable=draw(st.booleans()),
        writers=draw(st.lists(_IDENT, min_size=0, max_size=3)),
    )


@st.composite
def _function_gate(draw) -> FunctionGate:
    """Generate a FunctionGate varying names/modifiers/mutability/visibility."""
    name = draw(_IDENT)
    return FunctionGate(
        name=name,
        signature=f"{name}({draw(st.sampled_from(['', 'uint256', 'address,uint256']))})",
        visibility=draw(_VISIBILITY),
        mutability=draw(_MUTABILITY),
        modifier=draw(_MODIFIER),
        is_constructor=draw(st.booleans()),
        is_fallback=draw(st.booleans()),
        is_receive=draw(st.booleans()),
    )


@st.composite
def _contract(draw) -> FirstPartyContract:
    """Generate a FirstPartyContract with varied state vars and function gates."""
    name = draw(_IDENT)
    contract = FirstPartyContract(
        name=name,
        kind=draw(st.sampled_from(["contract", "interface", "library"])),
        source_file=f"{name}.sol",
    )
    contract.state_vars = draw(st.lists(_state_var(), min_size=0, max_size=3))
    contract.function_gates = draw(st.lists(_function_gate(), min_size=0, max_size=3))
    return contract


@st.composite
def _stage1_table(draw) -> Stage1Table:
    """Generate a NON-EMPTY Stage1Table (>=1 contract, no zero-contract short-circuit)."""
    table = Stage1Table(project_path=draw(st.sampled_from(["/proj", "/work/src", "/a"])))
    contracts = draw(st.lists(_contract(), min_size=1, max_size=4))
    # Dict keyed by contract name; distinct generated names collapse naturally,
    # but we keep at least one contract by construction (min_size=1).
    for c in contracts:
        table.contracts[c.name] = c
    return table


# Stage 2 invariants: {contract_name: {var_name: predicate}}.
_INVARIANTS = st.dictionaries(
    keys=_IDENT,
    values=st.dictionaries(
        keys=_IDENT,
        values=st.sampled_from(["monotonic", "nonzero", "bounded", "constant"]),
        min_size=0,
        max_size=3,
    ),
    min_size=0,
    max_size=3,
)

# Stage 3 CVL spec string.
_CVL = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=80,
).map(lambda s: f"rule r {{ {s} assert true; }}")


@st.composite
def _findings(draw) -> list:
    """Generate a stage-4 findings list (possibly empty)."""
    return draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "id": _IDENT,
                    "severity": st.sampled_from(["low", "medium", "high"]),
                    "message": st.text(
                        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
                        min_size=0,
                        max_size=40,
                    ),
                }
            ),
            min_size=0,
            max_size=3,
        )
    )


# Stage 5 report dict; the pipeline reads ``report["summary"]`` and persists the
# whole report as the stage-5 envelope payload.
_REPORT = st.fixed_dictionaries(
    {
        "summary": st.fixed_dictionaries(
            {
                "status": st.sampled_from(
                    ["tool_unavailable", "verified", "violated", "error"]
                ),
                "rules": st.integers(min_value=0, max_value=20),
            }
        ),
    }
)


class _StageBundle:
    """A deterministic bundle of stage outputs shared by both confluence paths."""

    def __init__(self, table, invariants, cvl, findings, applied_cvl, report):
        self.table = table
        self.invariants = invariants
        self.cvl = cvl
        self.findings = findings
        # The apply_findings RESULT is generated too and patched to a fixed value
        # so a non-empty findings list stays deterministic across both paths.
        self.applied_cvl = applied_cvl
        self.report = report

    @property
    def final_cvl(self) -> str:
        """The CVL spec that reaches stage 5: repaired when findings are non-empty."""
        return self.applied_cvl if self.findings else self.cvl


@st.composite
def _stage_bundle(draw) -> _StageBundle:
    return _StageBundle(
        table=draw(_stage1_table()),
        invariants=draw(_INVARIANTS),
        cvl=draw(_CVL),
        findings=draw(_findings()),
        applied_cvl=draw(_CVL),
        report=draw(_REPORT),
    )


# ---------------------------------------------------------------------------
# Patch / restore + payload helpers
# ---------------------------------------------------------------------------

# The pipeline stage callables patched onto the module for the confluence run.
_PATCHED_ATTRS = (
    "extract_first_party",
    "mine_invariants",
    "write_rules",
    "criticize",
    "apply_findings",
    "verify_with_prover",
)


def _install_bundle(bundle: _StageBundle) -> None:
    """Patch every stage callable to return fresh deep copies of *bundle*.

    Returning copies per call guarantees path A and path B observe identical
    (but independent) values, so neither path can mutate the other's data.
    """

    def _table():
        return copy.deepcopy(bundle.table)

    pipeline.extract_first_party = lambda *a, **k: _table()
    pipeline.mine_invariants = lambda *a, **k: copy.deepcopy(bundle.invariants)
    pipeline.write_rules = lambda *a, **k: bundle.cvl
    pipeline.criticize = lambda *a, **k: copy.deepcopy(bundle.findings)
    pipeline.apply_findings = lambda *a, **k: bundle.applied_cvl
    pipeline.verify_with_prover = lambda *a, **k: copy.deepcopy(bundle.report)


def _artifact_payload(output_dir: Path, base: str, stage: int) -> dict:
    """Return the ``payload`` section of a stage artifact envelope.

    Only the payload is compared; provenance fields that legitimately differ
    between two runs (completed_utc / duration / digest / run-id) live in the
    separate ``provenance`` section, which this helper never reads.
    """
    path = output_dir / f"{base}_stage{stage}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert "payload" in envelope, f"{path} missing payload section"
    return envelope["payload"]


# ---------------------------------------------------------------------------
# Property 15: one 5-stage run == five single-stage runs, across a range of
# generated deterministic stage-output bundles.
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(bundle=_stage_bundle())
def test_confluence_one_shot_equals_five_single_stage(bundle):
    # Save the current pipeline stage callables so this pure @given test can
    # restore them (no function-scoped fixture -> no HealthCheck warning).
    saved = {name: getattr(pipeline, name) for name in _PATCHED_ATTRS}
    out_a = Path(tempfile.mkdtemp(prefix="confluence_a_"))
    out_b = Path(tempfile.mkdtemp(prefix="confluence_b_"))
    src_dir = Path(tempfile.mkdtemp(prefix="confluence_src_"))
    try:
        _install_bundle(bundle)

        sol_path = src_dir / "Token.sol"
        sol_path.write_text("contract Token {}\n", encoding="utf-8")
        base = pipeline.artifact_base_name(sol_path)

        # Path A: one invocation running all five stages.
        results_a = pipeline.run_pipeline(
            sol_path, output_dir=out_a, stages=[1, 2, 3, 4, 5]
        )

        # Path B: five sequential single-stage invocations into a separate dir.
        returns_b = {
            stage: pipeline.run_single_stage(stage, sol_path, output_dir=out_b)
            for stage in (1, 2, 3, 4, 5)
        }

        # Stages 1-3: persisted artifact PAYLOAD equal across the two paths.
        for stage in (1, 2, 3):
            payload_a = _artifact_payload(out_a, base, stage)
            payload_b = _artifact_payload(out_b, base, stage)
            assert payload_a == payload_b, f"stage {stage} payload diverged"

        # Stage 3 payload carries the generated CVL spec verbatim.
        assert _artifact_payload(out_a, base, 3)["cvl_spec"] == bundle.cvl

        # Stage 4: path A persists ``{findings, cvl_spec}``; single-stage stage 4
        # returns the findings. The persisted spec is the repaired spec when
        # findings are non-empty, else the stage-3 spec.
        payload4_a = _artifact_payload(out_a, base, 4)
        assert payload4_a["findings"] == returns_b[4]
        assert payload4_a["findings"] == bundle.findings
        assert payload4_a["cvl_spec"] == bundle.final_cvl

        # Stage 5: path A persists the Verification_Report; single-stage stage 5
        # returns the same report object.
        payload5_a = _artifact_payload(out_a, base, 5)
        assert payload5_a == returns_b[5]
        assert payload5_a == bundle.report

        # Same terminal outcome + exit code. A five-stage run over a non-empty
        # table completes with no short-circuit outcome; path B reaching here
        # without a PipelineOutcome confirms the single-stage path agrees.
        assert results_a["outcome"] is None
        assert results_a["exit_code"] is None
    finally:
        for name, fn in saved.items():
            setattr(pipeline, name, fn)
        shutil.rmtree(out_a, ignore_errors=True)
        shutil.rmtree(out_b, ignore_errors=True)
        shutil.rmtree(src_dir, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
