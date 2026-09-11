"""Unit tests for deterministic Stage 1 extraction ordering (Requirement 21.6).

`FirstPartyExtractor.analyze()` used to iterate a Python ``set`` of first-party
names, so contract ordering (and thus serialized artifact bytes) was randomized
per process by hash seeding. Task 3.1 makes ordering deterministic by:

* iterating ``self.first_party_names`` in sorted order in ``analyze()``, and
* normalizing each contract's ``state_vars``/``function_gates``/``caller_edges``
  via the pure helper ``_ordered_contract``.

slither is not installable in this environment, so these tests exercise the
pure ordering helper and the ``Stage1Table`` serialization directly, bypassing
slither entirely. The module under test is loaded via importlib direct-file
load (mirroring tests/unit/test_artifacts_basics.py) so it runs offline even
though ``spec_pipeline/__init__.py`` eagerly imports the slither-backed stages.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

def _load_stage1_module():
    """Load ``spec_pipeline/stage1_extract.py`` offline.

    ``spec_pipeline/__init__.py`` and ``stage1_extract.py`` both eagerly import
    the slither-backed ``solidity_graph.analyzer`` at module load. That import
    is unrelated to the pure ordering logic under test and slither is not
    installable here, so we register a lightweight stub for
    ``solidity_graph.analyzer`` in ``sys.modules`` before loading the module by
    file path (the importlib fallback pattern from test_artifacts_basics.py).
    """
    import types

    stub = types.ModuleType("solidity_graph.analyzer")
    # Names stage1_extract imports from solidity_graph.analyzer. Only their
    # presence matters; the ordering helper never calls them.
    stub.SolidityAnalyzer = object
    stub.SolidityGraph = object
    stub.ContractInfo = object
    stub.FunctionNode = object
    stub._find_solc = lambda *a, **k: None
    stub._build_solc_remaps = lambda *a, **k: []

    pkg = types.ModuleType("solidity_graph")
    pkg.analyzer = stub
    pkg.__path__ = []  # mark as package

    mod_name = "spec_pipeline_stage1_under_test"
    saved = {
        k: sys.modules.get(k)
        for k in ("solidity_graph", "solidity_graph.analyzer", mod_name)
    }
    sys.modules["solidity_graph"] = pkg
    sys.modules["solidity_graph.analyzer"] = stub
    try:
        stage1_path = _REPO_ROOT / "spec_pipeline" / "stage1_extract.py"
        spec = importlib.util.spec_from_file_location(mod_name, stage1_path)
        mod = importlib.util.module_from_spec(spec)
        # Register before exec so @dataclass can resolve cls.__module__.
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


try:
    from spec_pipeline.stage1_extract import (  # type: ignore
        CallerEdge,
        FirstPartyContract,
        FunctionGate,
        Stage1Table,
        StateVarInfo,
        _ordered_contract,
    )
except Exception:  # pragma: no cover - fallback when slither deps absent
    _mod = _load_stage1_module()
    CallerEdge = _mod.CallerEdge
    FirstPartyContract = _mod.FirstPartyContract
    FunctionGate = _mod.FunctionGate
    Stage1Table = _mod.Stage1Table
    StateVarInfo = _mod.StateVarInfo
    _ordered_contract = _mod._ordered_contract


# ---------------------------------------------------------------------------
# Builders for unordered inputs (no slither involved)
# ---------------------------------------------------------------------------


def _unordered_contract(name: str) -> FirstPartyContract:
    """Build a FirstPartyContract whose list fields are in scrambled order."""
    fpc = FirstPartyContract(name=name, kind="contract", source_file=f"{name}.sol")
    # State vars deliberately out of name order.
    fpc.state_vars = [
        StateVarInfo("zulu", "uint256", "public", False, False, ["setZulu"]),
        StateVarInfo("alpha", "address", "internal", False, True, []),
        StateVarInfo("Beta", "bool", "private", True, False, []),
    ]
    # Function gates out of signature order; includes an overload.
    fpc.function_gates = [
        FunctionGate("transfer", "(address to, uint256 amt)", "external", "nonpayable", "onlyOwner"),
        FunctionGate("approve", "(address s, uint256 amt)", "public", "nonpayable", "none"),
        FunctionGate("transfer", "(address to)", "external", "nonpayable", "onlyOwner"),
    ]
    # Caller edges out of tuple order.
    fpc.caller_edges = [
        CallerEdge(name, "transfer", "Other", "notify", "external"),
        CallerEdge(name, "approve", name, "check", "internal"),
        CallerEdge(name, "transfer", "Other", "ack", "external"),
    ]
    return fpc


# ---------------------------------------------------------------------------
# _ordered_contract: per-contract collection ordering
# ---------------------------------------------------------------------------


def test_ordered_contract_sorts_state_vars_by_name():
    fpc = _ordered_contract(_unordered_contract("Token"))
    names = [sv.name for sv in fpc.state_vars]
    # Ascending Unicode code point: uppercase 'B' (66) sorts before lowercase.
    assert names == sorted(names)
    assert names == ["Beta", "alpha", "zulu"]


def test_ordered_contract_sorts_function_gates_by_signature():
    fpc = _ordered_contract(_unordered_contract("Token"))
    keys = [fg.name + fg.signature for fg in fpc.function_gates]
    assert keys == sorted(keys)
    # The two transfer overloads keep both entries, ordered by full signature.
    assert keys[0].startswith("approve")
    assert keys[1] == "transfer(address to)"
    assert keys[2] == "transfer(address to, uint256 amt)"


def test_ordered_contract_sorts_caller_edges_by_stable_tuple():
    fpc = _ordered_contract(_unordered_contract("Token"))
    tuples = [
        (e.caller_contract, e.caller_function, e.callee_contract, e.callee_function, e.call_type)
        for e in fpc.caller_edges
    ]
    assert tuples == sorted(tuples)


def test_ordered_contract_is_idempotent():
    once = _ordered_contract(_unordered_contract("Token"))
    once_json = json.dumps(once.__dict__, default=lambda o: o.__dict__, sort_keys=True)
    twice = _ordered_contract(once)
    twice_json = json.dumps(twice.__dict__, default=lambda o: o.__dict__, sort_keys=True)
    assert once_json == twice_json


def test_ordered_contract_preserves_field_names_and_shape():
    fpc = _ordered_contract(_unordered_contract("Token"))
    sv = fpc.state_vars[0]
    # Do not rename fields: StateVarInfo.type must still exist.
    assert hasattr(sv, "type")
    fg = fpc.function_gates[0]
    assert hasattr(fg, "signature")
    # No entries dropped: 3 in -> 3 out for each collection.
    assert len(fpc.state_vars) == 3
    assert len(fpc.function_gates) == 3
    assert len(fpc.caller_edges) == 3


# ---------------------------------------------------------------------------
# Table-level determinism: sorted contract keys + serialization stability
# ---------------------------------------------------------------------------


def _build_table_from_names(names: list[str]) -> Stage1Table:
    """Simulate analyze(): iterate names in sorted order, order each contract."""
    table = Stage1Table(project_path="/proj")
    for cname in sorted(names):
        table.contracts[cname] = _ordered_contract(_unordered_contract(cname))
    return table


def _serialize(table: Stage1Table) -> str:
    return json.dumps(table.to_json(), indent=2, sort_keys=True, default=str)


def test_table_contract_order_is_sorted_regardless_of_input_order():
    a = _build_table_from_names(["Zebra", "apple", "Mango", "banana"])
    b = _build_table_from_names(["banana", "Mango", "apple", "Zebra"])
    assert list(a.contracts.keys()) == sorted(a.contracts.keys())
    assert _serialize(a) == _serialize(b)


def test_table_serialization_stable_across_repeated_builds():
    names = ["Gamma", "alpha", "Delta", "beta"]
    first = _serialize(_build_table_from_names(names))
    second = _serialize(_build_table_from_names(list(reversed(names))))
    assert first == second


# ---------------------------------------------------------------------------
# Hash-seed independence: build the same table in two child processes with
# different PYTHONHASHSEED values and compare serialized output.
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = r"""
import importlib.util, json, sys, types
from pathlib import Path

# Stub the slither-backed analyzer so stage1_extract loads offline.
stub = types.ModuleType("solidity_graph.analyzer")
stub.SolidityAnalyzer = object
stub.SolidityGraph = object
stub.ContractInfo = object
stub.FunctionNode = object
stub._find_solc = lambda *a, **k: None
stub._build_solc_remaps = lambda *a, **k: []
pkg = types.ModuleType("solidity_graph")
pkg.analyzer = stub
pkg.__path__ = []
sys.modules["solidity_graph"] = pkg
sys.modules["solidity_graph.analyzer"] = stub

stage1 = Path(REPO_ROOT) / "spec_pipeline" / "stage1_extract.py"
spec = importlib.util.spec_from_file_location("s1", stage1)
m = importlib.util.module_from_spec(spec)
sys.modules["s1"] = m  # register before exec so @dataclass resolves __module__
spec.loader.exec_module(m)

names = ["Zebra", "apple", "Mango", "banana", "Alpha", "zeta"]
table = m.Stage1Table(project_path="/proj")
for cname in sorted(names):
    fpc = m.FirstPartyContract(name=cname, kind="contract", source_file=cname + ".sol")
    fpc.state_vars = [
        m.StateVarInfo("zulu", "uint256", "public", False, False, ["setZulu"]),
        m.StateVarInfo("alpha", "address", "internal", False, True, []),
        m.StateVarInfo("Beta", "bool", "private", True, False, []),
    ]
    fpc.function_gates = [
        m.FunctionGate("transfer", "(address to, uint256 amt)", "external", "nonpayable", "onlyOwner"),
        m.FunctionGate("approve", "(address s, uint256 amt)", "public", "nonpayable", "none"),
        m.FunctionGate("transfer", "(address to)", "external", "nonpayable", "onlyOwner"),
    ]
    fpc.caller_edges = [
        m.CallerEdge(cname, "transfer", "Other", "notify", "external"),
        m.CallerEdge(cname, "approve", cname, "check", "internal"),
        m.CallerEdge(cname, "transfer", "Other", "ack", "external"),
    ]
    table.contracts[cname] = m._ordered_contract(fpc)

print(json.dumps(table.to_json(), indent=2, sort_keys=True, default=str))
"""


def _run_child(hashseed: str) -> str:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    script = "REPO_ROOT = %r\n" % str(_REPO_ROOT) + _CHILD_SCRIPT
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"child failed (seed={hashseed}): {proc.stderr}"
    return proc.stdout


def test_serialized_table_identical_across_hash_seeds():
    """Two processes with different PYTHONHASHSEED produce identical output."""
    out0 = _run_child("0")
    out1 = _run_child("1")
    out_random = _run_child("random")
    assert out0 == out1 == out_random


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
