"""Unit tests for the generality refinements in task 13.1 (Requirements 19.3-19.6).

Covers four pure, slither-free refinements:

* R19.3 - ``stage2_invariants._is_initializer`` truth table and the
  initializer-aware ``classify_immutable_once_set`` classification, where a state
  variable written only by an ``initialize()`` / ``initializer``-guarded function
  (and/or the constructor) is immutable-once-set rather than ``free``.
* R19.4 - ``stage1_extract._detect_delegation`` marks a delegatecall-ing contract
  as a proxy and records one delegation edge per delegating function, naming the
  state variable holding the target (``unresolved`` when no single state var
  holds it). The new ``is_proxy`` / ``delegation_edges`` fields round-trip through
  the Artifact_Store.
* R19.5 - ``stage1_extract._library_scope`` classifies a library declared under a
  dependency root as ``dependency`` scope and a first-party library as
  ``first_party`` scope.
* R19.6 - the extractor signals a compile failure via ``CompileError`` and the
  orchestrator seam ``pipeline._compile_failed_outcome`` maps it to the
  ``compile_failed`` outcome (exit 7), tested at the pure-helper level.

slither is not installable here, so - mirroring
``tests/unit/test_artifacts_stage1_roundtrip.py`` and
``tests/unit/test_pipeline_zero_contract.py`` - we register slither-free stubs
for ``solidity_graph.analyzer`` and the parent packages BEFORE loading the
modules by file path, and stub the LLM-backed stage modules ``pipeline.py``
imports so the pipeline helpers load offline.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_stub_packages() -> None:
    """Register slither-free stubs so the stage modules load offline."""
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
    sys.modules[mod_name] = mod  # register before exec (dataclass __module__)
    spec.loader.exec_module(mod)
    return mod


_install_stub_packages()

# Stage 1 dataclasses + helpers (slither-free once analyzer is stubbed).
if "spec_pipeline.stage1_extract" not in sys.modules:
    _stage1 = _load_module_by_path(
        "spec_pipeline.stage1_extract", "spec_pipeline/stage1_extract.py"
    )
else:  # pragma: no cover - depends on collection order
    _stage1 = sys.modules["spec_pipeline.stage1_extract"]

CallerEdge = _stage1.CallerEdge
DelegationEdge = _stage1.DelegationEdge
FirstPartyContract = _stage1.FirstPartyContract
FunctionGate = _stage1.FunctionGate
Stage1Table = _stage1.Stage1Table
StateVarInfo = _stage1.StateVarInfo
CompileError = _stage1.CompileError
_detect_delegation = _stage1._detect_delegation
_library_scope = _stage1._library_scope

# Artifact_Store (de)serialization for the proxy round-trip.
if "spec_pipeline.artifacts" not in sys.modules:
    _artifacts = _load_module_by_path(
        "spec_pipeline.artifacts", "spec_pipeline/artifacts.py"
    )
else:  # pragma: no cover
    _artifacts = sys.modules["spec_pipeline.artifacts"]

serialize_stage1 = _artifacts.serialize_stage1
deserialize_stage1 = _artifacts.deserialize_stage1

# Stage 2 classification helpers. stage2_invariants imports llm_client / prompts
# / utils (all slither-free) and stage1_extract (already stubbed/loaded).
if "spec_pipeline.stage2_invariants" not in sys.modules:
    _stage2 = _load_module_by_path(
        "spec_pipeline.stage2_invariants", "spec_pipeline/stage2_invariants.py"
    )
else:  # pragma: no cover
    _stage2 = sys.modules["spec_pipeline.stage2_invariants"]

_is_initializer = _stage2._is_initializer
classify_immutable_once_set = _stage2.classify_immutable_once_set


# ---------------------------------------------------------------------------
# Duck-typed stand-in for a slither FunctionNode (only the fields the detector
# reads). Keeps the delegation detector test slither-free.
# ---------------------------------------------------------------------------


class _FakeFunc:
    def __init__(self, name, external_calls=None, state_vars_read=None):
        self.name = name
        self.external_calls = external_calls or []
        self.state_vars_read = state_vars_read or []


# ===========================================================================
# R19.3: _is_initializer truth table
# ===========================================================================


@pytest.mark.parametrize(
    "func_name,modifier,expected",
    [
        # Modifier-driven matches.
        ("setUp", "initializer", True),
        ("setUp", "reinitializer", True),
        ("setUp", "reinitializer(2)", True),  # parameterized reinitializer
        # Name-driven matches.
        ("initialize", "none", True),
        ("initialize", "onlyOwner", True),
        ("initializeV2", "none", True),
        ("initialize_pool", "none", True),
        # Non-initializers.
        ("constructor", "none", False),
        ("setConfig", "onlyOwner", False),
        ("transfer", "none", False),
        ("reinit", "none", False),  # "reinit" is not an "initialize" prefix
        # Case sensitivity: Solidity identifiers are case-sensitive.
        ("Initialize", "none", False),
        # Robust to empty / None inputs.
        ("", "", False),
        (None, None, False),
    ],
)
def test_is_initializer_truth_table(func_name, modifier, expected):
    assert _is_initializer(func_name, modifier) is expected


# ===========================================================================
# R19.3: initializer-as-writer immutable-once-set classification
# ===========================================================================


def _contract_with_gates(gates):
    fpc = FirstPartyContract(name="Vault", kind="contract", source_file="Vault.sol")
    fpc.function_gates = gates
    return fpc


def test_var_written_only_by_initialize_is_immutable_once_set():
    # `initialize()` writes `asset`; it is the only writer -> immutable-once-set.
    gates = [
        FunctionGate("initialize", "(address a)", "external", "nonpayable", "initializer"),
    ]
    contract = _contract_with_gates(gates)
    sv = StateVarInfo("asset", "address", "public", False, False, ["initialize"])
    assert classify_immutable_once_set(contract, sv) is True


def test_var_written_by_constructor_and_initializer_is_immutable_once_set():
    gates = [
        FunctionGate("constructor", "()", "public", "nonpayable", "none", is_constructor=True),
        FunctionGate("initialize", "(uint256 x)", "external", "nonpayable", "initializer"),
    ]
    contract = _contract_with_gates(gates)
    sv = StateVarInfo("owner", "address", "public", False, False,
                      ["constructor", "initialize"])
    assert classify_immutable_once_set(contract, sv) is True


def test_var_written_by_ordinary_setter_is_not_immutable_once_set():
    # A var written by a normal (non-initializer, non-constructor) function is
    # NOT immutable-once-set: it can change after setup.
    gates = [
        FunctionGate("setFee", "(uint256 f)", "external", "nonpayable", "onlyOwner"),
    ]
    contract = _contract_with_gates(gates)
    sv = StateVarInfo("fee", "uint256", "public", False, False, ["setFee"])
    assert classify_immutable_once_set(contract, sv) is False


def test_var_written_by_initializer_and_setter_is_not_immutable_once_set():
    # Mixed writers: one is an ordinary setter, so it is not immutable-once-set.
    gates = [
        FunctionGate("initialize", "(uint256 x)", "external", "nonpayable", "initializer"),
        FunctionGate("setFee", "(uint256 f)", "external", "nonpayable", "onlyOwner"),
    ]
    contract = _contract_with_gates(gates)
    sv = StateVarInfo("fee", "uint256", "public", False, False,
                      ["initialize", "setFee"])
    assert classify_immutable_once_set(contract, sv) is False


def test_never_written_var_is_not_immutable_once_set():
    contract = _contract_with_gates([])
    sv = StateVarInfo("cached", "uint256", "public", False, False, [])
    assert classify_immutable_once_set(contract, sv) is False


def test_initialize_named_writer_without_gate_modifier_still_counts():
    # The writer is named "initialize" but recorded with no gate modifier: the
    # name prefix alone qualifies it as an initializer (R19.3).
    gates = [
        FunctionGate("initialize", "()", "external", "nonpayable", "none"),
    ]
    contract = _contract_with_gates(gates)
    sv = StateVarInfo("token", "address", "public", False, False, ["initialize"])
    assert classify_immutable_once_set(contract, sv) is True


# ===========================================================================
# R19.4: proxy marking + delegation edge recording
# ===========================================================================


def test_delegation_detected_with_single_state_var_target():
    funcs = [
        _FakeFunc(
            "fallback",
            external_calls=[{"function_name": "delegatecall", "call_type": "LowLevelCall"}],
            state_vars_read=["implementation"],
        ),
    ]
    is_proxy, edges = _detect_delegation("Proxy", funcs)
    assert is_proxy is True
    assert len(edges) == 1
    assert edges[0].proxy_contract == "Proxy"
    assert edges[0].delegating_function == "fallback"
    assert edges[0].target_state_var == "implementation"


def test_delegation_target_unresolved_when_no_single_state_var():
    # Zero state vars read, or more than one, -> unresolved (conservative).
    funcs_zero = [
        _FakeFunc(
            "run",
            external_calls=[{"function_name": "delegatecall"}],
            state_vars_read=[],
        ),
    ]
    is_proxy, edges = _detect_delegation("Router", funcs_zero)
    assert is_proxy is True
    assert edges[0].target_state_var == "unresolved"

    funcs_many = [
        _FakeFunc(
            "dispatch",
            external_calls=[{"function_name": "delegatecall"}],
            state_vars_read=["a", "b"],
        ),
    ]
    _, edges_many = _detect_delegation("Router", funcs_many)
    assert edges_many[0].target_state_var == "unresolved"


def test_no_delegation_marks_not_proxy():
    funcs = [
        _FakeFunc(
            "transfer",
            external_calls=[{"function_name": "call"}],
            state_vars_read=["balance"],
        ),
    ]
    is_proxy, edges = _detect_delegation("Token", funcs)
    assert is_proxy is False
    assert edges == []


def test_one_edge_per_delegating_function():
    funcs = [
        _FakeFunc("f1", external_calls=[{"function_name": "delegatecall"}],
                  state_vars_read=["impl1"]),
        _FakeFunc("f2", external_calls=[{"function_name": "delegatecall"}],
                  state_vars_read=["impl2"]),
        _FakeFunc("g", external_calls=[{"function_name": "call"}]),
    ]
    is_proxy, edges = _detect_delegation("MultiProxy", funcs)
    assert is_proxy is True
    assert {e.delegating_function for e in edges} == {"f1", "f2"}


def test_proxy_fields_round_trip_through_artifact_store():
    table = Stage1Table(project_path="/proj")
    proxy = FirstPartyContract(name="Proxy", kind="contract", source_file="Proxy.sol")
    proxy.is_proxy = True
    proxy.delegation_edges = [
        DelegationEdge("Proxy", "fallback", "implementation"),
        DelegationEdge("Proxy", "upgradeToAndCall", "unresolved"),
    ]
    table.contracts["Proxy"] = proxy
    table.contracts["Plain"] = FirstPartyContract(
        name="Plain", kind="contract", source_file="Plain.sol"
    )

    payload = serialize_stage1(table)
    # serialize_stage1 stays lossless w.r.t. to_json (asdict), incl. new fields.
    assert payload == table.to_json()

    restored = deserialize_stage1(payload)
    assert restored.contracts["Proxy"].is_proxy is True
    assert restored.contracts["Plain"].is_proxy is False
    edges = restored.contracts["Proxy"].delegation_edges
    assert [(e.delegating_function, e.target_state_var) for e in edges] == [
        ("fallback", "implementation"),
        ("upgradeToAndCall", "unresolved"),
    ]
    # Round-trip is stable and text renders proxy marking.
    assert serialize_stage1(restored) == payload
    assert "Proxy: yes" in restored.to_text()


def test_legacy_artifact_without_proxy_fields_deserializes():
    # An artifact written before task 13.1 has no is_proxy / delegation_edges;
    # it must still load with defaults (backward compatible).
    legacy = {
        "project_path": "/proj",
        "contracts": {
            "Old": {
                "name": "Old",
                "kind": "contract",
                "source_file": "Old.sol",
                "state_vars": [],
                "function_gates": [],
                "caller_edges": [],
            }
        },
    }
    restored = deserialize_stage1(legacy)
    assert restored.contracts["Old"].is_proxy is False
    assert restored.contracts["Old"].delegation_edges == []


# ===========================================================================
# R19.5: library scope classification
# ===========================================================================


def test_library_under_dependency_root_is_dependency_scope(tmp_path):
    node_modules = tmp_path / "node_modules"
    lib_file = node_modules / "@openzeppelin" / "SafeMath.sol"
    lib_file.parent.mkdir(parents=True)
    lib_file.write_text("library SafeMath {}\n")

    assert _library_scope(str(lib_file), [node_modules]) == "dependency"


def test_first_party_library_is_first_party_scope(tmp_path):
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    fp_lib = tmp_path / "src" / "MathLib.sol"
    fp_lib.parent.mkdir(parents=True)
    fp_lib.write_text("library MathLib {}\n")

    # Declared in first-party source, not under any dep root -> first_party.
    assert _library_scope(str(fp_lib), [node_modules]) == "first_party"


def test_library_scope_empty_source_defaults_first_party():
    assert _library_scope("", [Path("/x/node_modules")]) == "first_party"


def test_library_scope_no_dep_roots_is_first_party(tmp_path):
    f = tmp_path / "Lib.sol"
    f.write_text("library Lib {}\n")
    assert _library_scope(str(f), []) == "first_party"


# ===========================================================================
# R19.6: compile_failed outcome reachable from the extractor signal
# ===========================================================================


def test_compile_error_carries_diagnostics():
    err = CompileError(
        diagnostics="ParserError: expected ';'",
        solc_version="0.8.19",
        remappings=["@oz/=node_modules/@oz/"],
    )
    assert err.diagnostics == "ParserError: expected ';'"
    assert err.solc_version == "0.8.19"
    assert err.remappings == ["@oz/=node_modules/@oz/"]
    # Message surfaces the version + diagnostics.
    assert "0.8.19" in str(err)
    assert "ParserError" in str(err)


def _load_pipeline_offline():
    """Load ``pipeline.py`` with LLM-backed stages stubbed (compile_failed seam)."""
    stubs = {
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
    saved: dict[str, object] = {}
    for name, attrs in stubs.items():
        saved[name] = sys.modules.get(name)
        # stage2_invariants is a real slither-free module we already loaded; keep
        # it, but ensure mine_invariants exists (it does). For the others install
        # lightweight stubs so pipeline.py binds them at import.
        if name == "spec_pipeline.stage2_invariants" and saved[name] is not None:
            continue
        mod = types.ModuleType(name)
        for attr, fn in attrs.items():
            setattr(mod, attr, fn)
        sys.modules[name] = mod

    unique = "spec_pipeline._pipeline_under_test_generality"
    try:
        if unique in sys.modules:  # pragma: no cover
            return sys.modules[unique]
        return _load_module_by_path(unique, "spec_pipeline/pipeline.py")
    finally:
        for name, prev in saved.items():
            if name == "spec_pipeline.stage2_invariants" and prev is not None:
                continue
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


_pipeline = _load_pipeline_offline()


def test_compile_failed_outcome_maps_to_exit_7():
    err = CompileError("boom", solc_version="0.7.6", remappings=[])
    po = _pipeline._compile_failed_outcome(err, Path("/proj/Bad.sol"))
    assert po.outcome == _pipeline.OUTCOME_COMPILE_FAILED
    assert po.outcome == "compile_failed"
    assert po.exit_code == 7
    assert po.exit_code == _pipeline.EXIT_COMPILE_FAILED
    # Message names the attempted solc + diagnostics.
    assert "0.7.6" in str(po)
    assert "boom" in str(po)


def test_run_single_stage_maps_compile_error_to_outcome(tmp_path, monkeypatch):
    sol = tmp_path / "Bad.sol"
    sol.write_text("contract Bad { this is not solidity }\n")

    def _boom_extract(*a, **k):
        raise CompileError("ParserError", solc_version="0.8.0", remappings=[])

    monkeypatch.setattr(_pipeline, "extract_first_party", _boom_extract)

    with pytest.raises(_pipeline.PipelineOutcome) as excinfo:
        _pipeline.run_single_stage(1, sol, output_dir=tmp_path / "out")

    assert excinfo.value.outcome == "compile_failed"
    assert excinfo.value.exit_code == 7


def test_run_pipeline_maps_compile_error_to_outcome(tmp_path, monkeypatch):
    sol = tmp_path / "Bad.sol"
    sol.write_text("contract Bad { nope }\n")

    def _boom_extract(*a, **k):
        raise CompileError("solc: no version", solc_version="unknown", remappings=[])

    monkeypatch.setattr(_pipeline, "extract_first_party", _boom_extract)

    with pytest.raises(_pipeline.PipelineOutcome) as excinfo:
        _pipeline.run_pipeline(sol, output_dir=tmp_path / "out", stages=[1, 2, 3])

    assert excinfo.value.outcome == "compile_failed"
    assert excinfo.value.exit_code == 7
    # No Stage 1 artifact was written (stopped before the envelope write).
    assert not (tmp_path / "out" / "Bad_stage1.json").exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
