"""Unit test for ``FirstPartyExtractor.export_json`` routing through Artifact_Store.

Covers task 3.2 (Requirements 2.5, 21.7):

``export_json`` must write the Stage 1 table's JSON payload produced by
:func:`spec_pipeline.artifacts.serialize_stage1` (so the extractor's own export
never diverges from the Artifact_Store's canonical payload), in canonical form
(sorted keys, two-space indent, ``ensure_ascii=False``, exactly one trailing
newline), and the written bytes must round-trip back through
:func:`spec_pipeline.artifacts.deserialize_stage1` to a table rendering the same
``to_text()``.

slither is not installable in this environment, so we reuse the same slither-free
stub + importlib loading pattern as ``test_artifacts_stage1_roundtrip.py``:
register slither-free parent-package stubs and a ``solidity_graph.analyzer`` stub
in ``sys.modules`` BEFORE importing ``spec_pipeline.stage1_extract`` (whose module
import eagerly pulls in the slither-backed analyzer), then load the modules by
file path via importlib.
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
    """Load a module by file path, registering it before exec_module."""
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod  # register before exec (dataclass __module__)
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
FirstPartyExtractor = _stage1.FirstPartyExtractor
FunctionGate = _stage1.FunctionGate
Stage1Table = _stage1.Stage1Table
StateVarInfo = _stage1.StateVarInfo


try:
    from spec_pipeline.artifacts import (  # type: ignore
        deserialize_stage1,
        serialize_stage1,
    )
except Exception:  # pragma: no cover - fallback when spec_pipeline.__init__ deps absent
    _artifacts = _load_module_by_path(
        "spec_pipeline.artifacts", "spec_pipeline/artifacts.py"
    )
    deserialize_stage1 = _artifacts.deserialize_stage1
    serialize_stage1 = _artifacts.serialize_stage1


def _rich_table() -> Stage1Table:
    table = Stage1Table(project_path="/proj/contracts")

    token = FirstPartyContract(name="Token", kind="contract", source_file="Token.sol")
    token.state_vars = [
        StateVarInfo("balance", "uint256", "public", False, False, ["transfer", "mint"]),
        StateVarInfo("owner", "address", "internal", False, True, []),
        StateVarInfo("MAX", "uint256", "private", True, False, []),
    ]
    token.function_gates = [
        FunctionGate("transfer", "(address to, uint256 amt)", "external", "nonpayable", "onlyOwner"),
        FunctionGate("transfer", "(address to)", "external", "nonpayable", "onlyOwner"),
        FunctionGate("totalSupply", "() -> uint256", "public", "view", "none"),
    ]
    token.caller_edges = [
        CallerEdge("Token", "transfer", "Vault", "notify", "external"),
        CallerEdge("Token", "mint", "Token", "_beforeMint", "internal"),
    ]

    # Unicode identifiers exercise ensure_ascii=False.
    vault = FirstPartyContract(name="Vault", kind="contract", source_file="Vault.sol")
    vault.state_vars = [
        StateVarInfo("sold\u00e9", "uint256", "public", False, False, ["dep\u00f3sito"]),
    ]
    vault.caller_edges = [
        CallerEdge("Vault", "dep\u00f3sito", "Token", "transfer", "external"),
    ]

    table.contracts["Token"] = token
    table.contracts["Vault"] = vault
    return table


def _make_extractor() -> FirstPartyExtractor:
    """Build an extractor without triggering slither-backed __init__ work."""
    ext = FirstPartyExtractor.__new__(FirstPartyExtractor)
    return ext


def test_export_json_writes_serialize_stage1_payload(tmp_path):
    table = _rich_table()
    out = tmp_path / "Token_stage1.json"

    _make_extractor().export_json(table, out)

    loaded = json.loads(out.read_text(encoding="utf-8"))
    # The written JSON equals the Artifact_Store canonical payload, NOT an
    # independent envelope (no top-level provenance/payload wrapper).
    assert loaded == serialize_stage1(table)
    assert "provenance" not in loaded


def test_export_json_is_canonical(tmp_path):
    table = _rich_table()
    out = tmp_path / "Token_stage1.json"

    _make_extractor().export_json(table, out)

    text = out.read_text(encoding="utf-8")
    # Sorted keys + two-space indent + trailing newline match _canonical_json.
    expected = json.dumps(
        serialize_stage1(table), sort_keys=True, indent=2, ensure_ascii=False
    ) + "\n"
    assert text == expected
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    # Non-ASCII preserved literally rather than \u-escaped (ensure_ascii=False).
    assert "sold\u00e9" in text


def test_export_json_round_trips_through_deserialize_stage1(tmp_path):
    table = _rich_table()
    out = tmp_path / "Token_stage1.json"

    _make_extractor().export_json(table, out)

    with open(out, encoding="utf-8") as f:
        restored = deserialize_stage1(json.load(f))

    assert restored.to_text() == table.to_text()
    # And the reconstructed table re-serializes to the same payload.
    assert serialize_stage1(restored) == serialize_stage1(table)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
