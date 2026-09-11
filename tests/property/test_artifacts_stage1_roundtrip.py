"""Property test for the Stage 1 artifact round-trip (design Property 1, task 2.4).

**Validates: Requirements 2.3, 2.4**

**Property 1: Stage 1 artifact round-trip.** For any generated
:class:`Stage1Table`, ``serialize -> deserialize -> serialize`` is byte-identical
(structurally equal dict), the deserialized table renders character-identically
(``to_text()`` equal to the original), and ``serialize_stage1(t)`` equals
``t.to_json()`` (the serialization is lossless). This is the hypothesis
(``@given``, >= 100 examples) counterpart to the example-based unit test in
``tests/unit/test_artifacts_stage1_roundtrip.py`` (which stays as-is).

Offline loading pattern
-----------------------
``artifacts.py`` stays slither-free by importing the Stage 1 dataclasses lazily
inside its functions, so ``deserialize_stage1``'s lazy
``from spec_pipeline.stage1_extract import ...`` must resolve to a slither-free
module. slither is not installable here, and the real parent ``__init__`` /
``analyzer`` modules eagerly import it. We therefore reuse the exact offline
stub pattern from the unit test: register lightweight ``spec_pipeline`` /
``solidity_graph`` package stubs plus a ``solidity_graph.analyzer`` stub in
``sys.modules``, then load ``stage1_extract`` (and, if needed, ``artifacts``) by
file path under their canonical dotted names BEFORE anything imports them.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_stub_packages() -> None:
    """Register slither-free stubs so ``spec_pipeline.stage1_extract`` loads offline.

    Mirrors ``tests/unit/test_artifacts_stage1_roundtrip.py``: pre-register
    lightweight *package* modules for ``spec_pipeline`` and ``solidity_graph``
    (with ``__path__`` set to the real dirs so their slither-free submodules
    still load by dotted name), bypassing their heavy ``__init__.py``, plus a
    ``solidity_graph.analyzer`` stub exposing only the names ``stage1_extract``
    imports (none are called by the serialization logic under test).
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


# ---------------------------------------------------------------------------
# Generators (inlined here — no shared tests/property/generators.py exists).
#
# Coverage targeted by the strategy below (design task 1.2 generator coverage):
#   * zero contracts (empty table)
#   * non-ASCII identifiers (unicode names / writers / signatures)
#   * length-1 identifiers
#   * duplicate function names across contracts
#   * overloaded names within one contract (same name, different signature)
#   * struct / enum / array-typed params (in signatures) and state var types
#   * functions with no modifier ("none")
#   * proxies with delegation_edges
#   * state vars with writers
# ---------------------------------------------------------------------------

# Identifier alphabet: mixes ASCII with non-ASCII (Latin-1 + a CJK/greek range)
# while excluding characters that would collide with the to_text() layout
# tokens or JSON escaping concerns. min_size=1 covers length-1 identifiers.
_ident = st.text(
    alphabet=st.characters(
        min_codepoint=0x21,
        max_codepoint=0x2FFF,
        blacklist_characters='"\\\n\r\t',
        # Drop the layout separators to keep to_text() unambiguous. These are
        # cosmetic constraints on the generated identifiers, not on the round
        # trip: the property holds for any string, but readable identifiers keep
        # counterexamples legible.
        blacklist_categories=("Cs",),
    ),
    min_size=1,
    max_size=8,
)

# Solidity-ish type strings including struct/enum names and array/mapping forms.
_type = st.sampled_from(
    [
        "uint256",
        "address",
        "bool",
        "uint256[]",  # array
        "address[3]",  # fixed array
        "bytes32",
        "MyStruct",  # struct-typed
        "Status",  # enum-typed
        "mapping(address => uint256)",
        "string",
    ]
)

_visibility_sv = st.sampled_from(["public", "internal", "private"])
_visibility_fn = st.sampled_from(["public", "external"])
_mutability = st.sampled_from(["view", "pure", "payable", "nonpayable"])
# "none" (no modifier) is included alongside real access gates.
_modifier = st.sampled_from(["none", "onlyOwner", "onlyRole", "auth", "nonReentrant"])


@st.composite
def _state_vars(draw):
    """A StateVarInfo, sometimes with a non-empty writers list (unicode-capable)."""
    return StateVarInfo(
        name=draw(_ident),
        type=draw(_type),
        visibility=draw(_visibility_sv),
        is_constant=draw(st.booleans()),
        is_immutable=draw(st.booleans()),
        writers=draw(st.lists(_ident, max_size=3)),
    )


@st.composite
def _signature(draw):
    """Build a param-list signature covering struct/enum/array-typed params."""
    params = draw(st.lists(st.tuples(_type, _ident), max_size=3))
    inner = ", ".join(f"{t} {n}" for (t, n) in params)
    sig = f"({inner})"
    if draw(st.booleans()):
        returns = draw(st.lists(_type, min_size=1, max_size=2))
        sig += f" -> {', '.join(returns)}"
    return sig


@st.composite
def _function_gates(draw, name_pool):
    """A FunctionGate; name drawn from a per-contract pool so overloads recur."""
    return FunctionGate(
        name=draw(st.sampled_from(name_pool)),
        signature=draw(_signature()),
        visibility=draw(_visibility_fn),
        mutability=draw(_mutability),
        modifier=draw(_modifier),
        is_constructor=draw(st.booleans()),
        is_fallback=draw(st.booleans()),
        is_receive=draw(st.booleans()),
    )


@st.composite
def _caller_edges(draw, contract_names):
    return CallerEdge(
        caller_contract=draw(st.sampled_from(contract_names)),
        caller_function=draw(_ident),
        callee_contract=draw(st.sampled_from(contract_names)),
        callee_function=draw(_ident),
        call_type=draw(st.sampled_from(["internal", "external", "library"])),
    )


@st.composite
def _delegation_edges(draw, proxy_name):
    return DelegationEdge(
        proxy_contract=proxy_name,
        delegating_function=draw(_ident),
        target_state_var=draw(st.one_of(st.just("unresolved"), _ident)),
    )


@st.composite
def _first_party_contract(draw, name, contract_names):
    # Per-contract function-name pool: small so overloaded names (same name,
    # different signature) recur within one contract.
    name_pool = draw(st.lists(_ident, min_size=1, max_size=3))
    is_proxy = draw(st.booleans())
    delegation_edges = (
        draw(st.lists(_delegation_edges(name), min_size=1, max_size=3))
        if is_proxy
        else []
    )
    return FirstPartyContract(
        name=name,
        kind=draw(st.sampled_from(["contract", "interface", "library"])),
        source_file=draw(_ident).__add__(".sol"),
        state_vars=draw(st.lists(_state_vars(), max_size=4)),
        function_gates=draw(st.lists(_function_gates(name_pool), max_size=5)),
        caller_edges=draw(st.lists(_caller_edges(contract_names), max_size=4)),
        is_proxy=is_proxy,
        delegation_edges=delegation_edges,
    )


@st.composite
def stage1_tables(draw):
    """Generate a Stage1Table covering the task-1.2 generator coverage.

    Includes zero-contract tables, non-ASCII / length-1 identifiers, duplicate
    function names across contracts (the contract keys differ but their internal
    function name pools overlap by construction), overloaded names within one
    contract, struct/enum/array-typed params, no-modifier functions, proxies with
    delegation edges, and state vars with writers.
    """
    project_path = draw(_ident)
    # Distinct contract keys; min_size=0 covers the zero-contract table.
    contract_names = draw(
        st.lists(_ident, min_size=0, max_size=4, unique=True)
    )

    table = Stage1Table(project_path=project_path)
    if not contract_names:
        return table

    for cname in contract_names:
        table.contracts[cname] = draw(
            _first_party_contract(cname, contract_names)
        )
    return table


# ---------------------------------------------------------------------------
# Property 1: Stage 1 artifact round-trip (Requirements 2.3, 2.4).
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(table=stage1_tables())
def test_stage1_artifact_round_trip(table):
    once = serialize_stage1(table)

    # serialize_stage1 is lossless: it agrees with to_json() (asdict) field for
    # field, so the artifact carries every field (Requirement 2.4).
    assert once == table.to_json()

    restored = deserialize_stage1(once)

    # serialize -> deserialize -> serialize is byte-identical (structural dict
    # equality; canonical JSON is a stable function of this dict) (R2.3).
    twice = serialize_stage1(restored)
    assert once == twice

    # The deserialized table renders character-identically (R2.4).
    assert restored.to_text() == table.to_text()


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
