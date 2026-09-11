"""Unit tests for Stage 1 typed (de)serialization in the Artifact_Store.

Covers task 2.3 (Requirements 2.1, 2.2, 2.6):

* :func:`spec_pipeline.artifacts.serialize_stage1`
* :func:`spec_pipeline.artifacts.deserialize_stage1`

The pair must round-trip: ``serialize -> deserialize -> serialize`` is
structurally identical, and a deserialized table renders the same ``to_text()``
as the original. A missing or wrong-typed declared field raises ``ArtifactError``
naming the artifact context, the contract, and the offending field.

slither is not installable in this environment. ``artifacts.py`` stays
slither-free by importing the Stage 1 dataclasses lazily inside its functions,
so we must stub ``solidity_graph.analyzer`` (and provide slither-free parent
package stubs for ``spec_pipeline`` and ``solidity_graph``) in ``sys.modules``
BEFORE anything imports ``spec_pipeline.stage1_extract`` (whose module import
eagerly pulls in the slither-backed analyzer, and whose real parent ``__init__``
modules also import slither). We register those stubs at import time here, then
load the modules by file path via importlib (registering each in ``sys.modules``
before ``exec_module`` so ``@dataclass`` under ``from __future__ import
annotations`` can resolve ``cls.__module__``).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_stub_packages() -> None:
    """Register slither-free stubs so ``spec_pipeline.stage1_extract`` loads offline.

    Loading ``spec_pipeline.stage1_extract`` under its real dotted name causes
    Python to import the parent packages ``spec_pipeline`` and
    ``solidity_graph`` first. Both real ``__init__``/``analyzer`` modules eagerly
    import slither, which is uninstallable here. We pre-register:

    * lightweight *package* modules for ``spec_pipeline`` and ``solidity_graph``
      (with ``__path__`` set to the real dirs so their submodules still load by
      dotted name), bypassing their heavy ``__init__.py``; and
    * a ``solidity_graph.analyzer`` stub exposing only the names
      ``stage1_extract`` imports (none are called by the logic under test).
    """
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
    sys.modules[mod_name] = mod  # register before exec (dataclass __module__)
    spec.loader.exec_module(mod)
    return mod


# Stubs must be in place before anything imports spec_pipeline.stage1_extract
# (directly here, or lazily inside artifacts.deserialize_stage1).
_install_stub_packages()

# Preload the slither-free stage1_extract under its canonical dotted name so the
# lazy `from spec_pipeline.stage1_extract import ...` inside deserialize_stage1
# resolves to it. stage1_extract itself imports spec_pipeline.resolve, which is
# slither-free and loads via the stubbed spec_pipeline package __path__.
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


try:
    from spec_pipeline.artifacts import (  # type: ignore
        ArtifactError,
        deserialize_stage1,
        serialize_stage1,
    )
except Exception:  # pragma: no cover - fallback when spec_pipeline.__init__ deps absent
    _artifacts = _load_module_by_path(
        "spec_pipeline.artifacts", "spec_pipeline/artifacts.py"
    )
    ArtifactError = _artifacts.ArtifactError
    deserialize_stage1 = _artifacts.deserialize_stage1
    serialize_stage1 = _artifacts.serialize_stage1


# ---------------------------------------------------------------------------
# Rich table builder (multiple contracts, empty + non-empty collections,
# overloaded functions, unicode identifiers)
# ---------------------------------------------------------------------------


def _rich_table() -> Stage1Table:
    table = Stage1Table(project_path="/proj/contracts")

    # Contract 1: full collections, an overloaded function, a writer list.
    token = FirstPartyContract(
        name="Token", kind="contract", source_file="Token.sol"
    )
    token.state_vars = [
        StateVarInfo("balance", "uint256", "public", False, False, ["transfer", "mint"]),
        StateVarInfo("owner", "address", "internal", False, True, []),
        StateVarInfo("MAX", "uint256", "private", True, False, []),
    ]
    token.function_gates = [
        FunctionGate(
            "transfer", "(address to, uint256 amt)", "external", "nonpayable",
            "onlyOwner",
        ),
        # overloaded name within one contract
        FunctionGate(
            "transfer", "(address to)", "external", "nonpayable", "onlyOwner",
        ),
        FunctionGate(
            "totalSupply", "() -> uint256", "public", "view", "none",
        ),
        FunctionGate(
            "", "()", "external", "payable", "none", is_receive=True,
        ),
    ]
    token.caller_edges = [
        CallerEdge("Token", "transfer", "Vault", "notify", "external"),
        CallerEdge("Token", "mint", "Token", "_beforeMint", "internal"),
    ]

    # Contract 2: unicode identifiers, empty collections mixed in.
    vault = FirstPartyContract(
        name="Vault", kind="contract", source_file="Vault.sol"
    )
    vault.state_vars = [
        StateVarInfo("sold\u00e9", "uint256", "public", False, False, ["dep\u00f3sito"]),
    ]
    vault.function_gates = []  # empty collection
    vault.caller_edges = [
        CallerEdge("Vault", "dep\u00f3sito", "Token", "transfer", "external"),
    ]

    # Contract 3: everything empty (edge case).
    empty = FirstPartyContract(
        name="\u00c9mpty", kind="interface", source_file="\u00c9mpty.sol"
    )

    table.contracts["Token"] = token
    table.contracts["Vault"] = vault
    table.contracts["\u00c9mpty"] = empty
    return table


def _empty_table() -> Stage1Table:
    return Stage1Table(project_path="/proj/empty")


# ---------------------------------------------------------------------------
# Round-trip: serialize -> deserialize -> serialize structural equality
# (Requirements 2.1, 2.2)
# ---------------------------------------------------------------------------


def test_serialize_deserialize_serialize_is_structurally_identical():
    original = _rich_table()
    once = serialize_stage1(original)
    restored = deserialize_stage1(once)
    twice = serialize_stage1(restored)
    assert once == twice


def test_deserialize_reconstructs_typed_values_not_empty_table():
    original = _rich_table()
    restored = deserialize_stage1(serialize_stage1(original))

    # Not an empty table (this is the empty-table defect being fixed).
    assert len(restored.contracts) == 3
    assert set(restored.contracts) == {"Token", "Vault", "\u00c9mpty"}

    token = restored.contracts["Token"]
    assert isinstance(token, FirstPartyContract)
    assert all(isinstance(sv, StateVarInfo) for sv in token.state_vars)
    assert all(isinstance(fg, FunctionGate) for fg in token.function_gates)
    assert all(isinstance(e, CallerEdge) for e in token.caller_edges)

    # Typed scalar values preserved, including booleans.
    max_var = next(sv for sv in token.state_vars if sv.name == "MAX")
    assert max_var.is_constant is True
    assert max_var.is_immutable is False
    balance = next(sv for sv in token.state_vars if sv.name == "balance")
    assert balance.writers == ["transfer", "mint"]

    # Overloaded function name kept as two distinct gates.
    transfers = [fg for fg in token.function_gates if fg.name == "transfer"]
    assert len(transfers) == 2
    assert {fg.signature for fg in transfers} == {
        "(address to, uint256 amt)",
        "(address to)",
    }

    receive_gate = next(fg for fg in token.function_gates if fg.is_receive)
    assert receive_gate.is_receive is True


def test_deserialized_table_renders_identical_to_text():
    original = _rich_table()
    restored = deserialize_stage1(serialize_stage1(original))
    assert restored.to_text() == original.to_text()


def test_empty_table_round_trips_to_zero_contracts():
    original = _empty_table()
    payload = serialize_stage1(original)
    assert payload["contracts"] == {}
    restored = deserialize_stage1(payload)
    assert len(restored.contracts) == 0
    assert restored.project_path == "/proj/empty"
    assert serialize_stage1(restored) == payload
    assert restored.to_text() == original.to_text()


def test_serialize_matches_to_json_shape():
    # serialize_stage1 must be lossless; to_json uses asdict and emits every
    # field, so the two must agree for a rich table.
    original = _rich_table()
    assert serialize_stage1(original) == original.to_json()


# ---------------------------------------------------------------------------
# Error handling: missing / wrong-typed fields raise ArtifactError naming
# context + contract + field (Requirement 2.6)
# ---------------------------------------------------------------------------


def test_missing_top_level_field_raises_naming_context():
    with pytest.raises(ArtifactError) as exc:
        deserialize_stage1({"project_path": "/p"}, context="/artifacts/Pool_stage1.json")
    msg = str(exc.value)
    assert "/artifacts/Pool_stage1.json" in msg
    assert "contracts" in msg


def test_missing_contract_field_raises_naming_contract_and_field():
    payload = {
        "project_path": "/p",
        "contracts": {
            "Token": {
                # 'kind' omitted
                "name": "Token",
                "source_file": "Token.sol",
                "state_vars": [],
                "function_gates": [],
                "caller_edges": [],
            }
        },
    }
    with pytest.raises(ArtifactError) as exc:
        deserialize_stage1(payload, context="art")
    msg = str(exc.value)
    assert "art" in msg
    assert "Token" in msg
    assert "kind" in msg


def test_wrong_typed_state_var_boolean_raises_naming_field():
    payload = {
        "project_path": "/p",
        "contracts": {
            "Token": {
                "name": "Token",
                "kind": "contract",
                "source_file": "Token.sol",
                "state_vars": [
                    {
                        "name": "x",
                        "type": "uint256",
                        "visibility": "public",
                        # is_constant should be a boolean, not a string
                        "is_constant": "yes",
                        "is_immutable": False,
                        "writers": [],
                    }
                ],
                "function_gates": [],
                "caller_edges": [],
            }
        },
    }
    with pytest.raises(ArtifactError) as exc:
        deserialize_stage1(payload, context="art")
    msg = str(exc.value)
    assert "Token" in msg
    assert "is_constant" in msg


def test_wrong_typed_contracts_container_raises():
    with pytest.raises(ArtifactError) as exc:
        deserialize_stage1({"project_path": "/p", "contracts": []}, context="art")
    assert "contracts" in str(exc.value)


def test_wrong_typed_caller_edge_field_raises_naming_field():
    payload = {
        "project_path": "/p",
        "contracts": {
            "Token": {
                "name": "Token",
                "kind": "contract",
                "source_file": "Token.sol",
                "state_vars": [],
                "function_gates": [],
                "caller_edges": [
                    {
                        "caller_contract": "Token",
                        "caller_function": "f",
                        "callee_contract": "Token",
                        # callee_function should be a string
                        "callee_function": 123,
                        "call_type": "internal",
                    }
                ],
            }
        },
    }
    with pytest.raises(ArtifactError) as exc:
        deserialize_stage1(payload, context="art")
    msg = str(exc.value)
    assert "Token" in msg
    assert "callee_function" in msg


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
