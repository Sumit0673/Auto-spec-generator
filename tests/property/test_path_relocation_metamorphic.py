"""Property-based test for the path-relocation metamorphic property (R7.7).

Property 11 (design): relocating a project to an unrelated ABSOLUTE path yields
FIELD-EQUAL Stage 1 artifacts AFTER rewriting recorded paths relative to the
analyzed root. In other words, an artifact's fields are invariant under moving
the project to a different absolute location, once every recorded absolute path
is expressed relative to the analyzed root.

The only path-bearing fields the Artifact_Store records for Stage 1 are the
table's ``project_path`` (the analyzed root) and each contract's
``source_file``. Every other field (contract/state-var/function/edge metadata)
carries no absolute location, so it must be byte-for-byte identical between the
original and the relocated project. This test builds a Stage 1 table rooted at
an absolute path A, constructs the exact equivalent rooted at an unrelated
absolute path B, serializes both through the real
``artifacts.serialize_stage1``, rewrites the two path-bearing fields relative to
their respective analyzed roots, and asserts the rewritten payloads are equal.

slither / solc / certoraRun / the network are all absent here. slither is not
installable, so ``solidity_graph.analyzer`` (and the ``spec_pipeline`` /
``solidity_graph`` parent packages) are stubbed in ``sys.modules`` under unique
private names BEFORE ``spec_pipeline.stage1_extract`` is loaded by file path
(the offline pattern used by tests/property/test_repair_loop_properties.py and
tests/unit/test_artifacts_stage1_roundtrip.py). Nothing here invokes a real
tool or opens a socket.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import PurePosixPath

from hypothesis import given, settings
from hypothesis import strategies as st

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Offline module loading (slither-free stubs + private module names)
# ---------------------------------------------------------------------------


def _install_stub_packages() -> None:
    """Register slither-free stubs so the stage-1 module loads offline."""
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

# stage1_extract must load under its canonical dotted name so the lazy
# `from spec_pipeline.stage1_extract import ...` inside serialize_stage1's helper
# resolves to the same module object.
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
    from spec_pipeline.artifacts import serialize_stage1  # type: ignore
except Exception:  # pragma: no cover - spec_pipeline.__init__ deps absent
    _artifacts = _load_module_by_path(
        "spec_pipeline.artifacts", "spec_pipeline/artifacts.py"
    )
    serialize_stage1 = _artifacts.serialize_stage1


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
#
# A Stage 1 table is generated as a set of contracts whose ``source_file`` is a
# RELATIVE posix subpath (never absolute). The two path-bearing fields are then
# rooted under an absolute analyzed root at build time: ``project_path`` is the
# root, and each ``source_file`` is ``root / relpath``. The same relative
# subpaths, rooted at a DIFFERENT absolute root, form the relocated project.

# Identifiers include non-ASCII, length-1, and multi-char names (R23.1 shapes).
_IDENT = st.one_of(
    st.text(
        alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
        min_size=1,
        max_size=8,
    ),
    st.sampled_from(["x", "\u00e9tat", "\u0441\u0447\u0451\u0442", "A", "_"]),
)

# Relative source-file subpaths as posix strings (may nest into subdirs).
_REL_SUBPATH = st.builds(
    lambda parts, stem: "/".join(parts + [f"{stem}.sol"]),
    st.lists(
        st.text(
            alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
            min_size=1,
            max_size=5,
        ),
        min_size=0,
        max_size=3,
    ),
    st.text(
        alphabet=st.characters(min_codepoint=ord("A"), max_codepoint=ord("z")),
        min_size=1,
        max_size=6,
    ),
)

_VISIBILITY = st.sampled_from(["public", "external", "internal", "private"])
_MUTABILITY = st.sampled_from(["view", "pure", "payable", "nonpayable"])
_PARAM_TYPE = st.sampled_from(
    ["uint256", "address", "bool", "bytes32", "uint256[]", "MyStruct", "MyEnum"]
)


@st.composite
def _state_var(draw) -> StateVarInfo:
    return StateVarInfo(
        name=draw(_IDENT),
        type=draw(_PARAM_TYPE),
        visibility=draw(_VISIBILITY),
        is_constant=draw(st.booleans()),
        is_immutable=draw(st.booleans()),
        writers=draw(st.lists(_IDENT, min_size=0, max_size=3)),
    )


@st.composite
def _function_gate(draw) -> FunctionGate:
    n_params = draw(st.integers(min_value=0, max_value=3))
    params = ", ".join(f"{draw(_PARAM_TYPE)} p{i}" for i in range(n_params))
    ret = draw(st.sampled_from(["", "bool", "uint256", "address"]))
    sig = "(" + params + ")"
    if ret:
        sig += f" -> {ret}"
    return FunctionGate(
        name=draw(_IDENT),
        signature=sig,
        visibility=draw(st.sampled_from(["public", "external"])),
        mutability=draw(_MUTABILITY),
        modifier=draw(st.sampled_from(["none", "onlyOwner", "auth"])),
    )


@st.composite
def _caller_edge(draw, contract_names) -> CallerEdge:
    other = draw(st.sampled_from(contract_names)) if contract_names else draw(_IDENT)
    return CallerEdge(
        caller_contract=draw(st.sampled_from(contract_names)) if contract_names
        else draw(_IDENT),
        caller_function=draw(_IDENT),
        callee_contract=other,
        callee_function=draw(_IDENT),
        call_type=draw(st.sampled_from(["internal", "external"])),
    )


@st.composite
def _relocatable_table(draw):
    """Build a Stage 1 table description independent of any absolute root.

    Returns ``(project_rel_marker, contract_specs)`` where each contract spec
    carries a RELATIVE ``source_file`` subpath. The caller roots the description
    at a chosen absolute root to build a concrete :class:`Stage1Table`.
    """
    cnames = draw(st.lists(_IDENT, min_size=0, max_size=3, unique=True))
    specs = []
    for cname in cnames:
        specs.append(
            {
                "name": cname,
                "kind": draw(st.sampled_from(["contract", "interface", "library"])),
                "rel_source": draw(_REL_SUBPATH),
                "state_vars": draw(st.lists(_state_var(), min_size=0, max_size=3)),
                "function_gates": draw(
                    st.lists(_function_gate(), min_size=0, max_size=3)
                ),
                "caller_edges": draw(
                    st.lists(_caller_edge(cnames), min_size=0, max_size=3)
                ),
            }
        )
    return specs


def _build_table(specs, root: str) -> Stage1Table:
    """Root a table description at an absolute POSIX *root*.

    ``project_path`` becomes *root* and each contract's ``source_file`` becomes
    the absolute path ``root / rel_source``. Every non-path field is copied
    verbatim, so any difference in the rewritten serialized payload can only
    come from the path fields.
    """
    table = Stage1Table(project_path=root)
    for spec in specs:
        abs_source = str(PurePosixPath(root) / spec["rel_source"])
        contract = FirstPartyContract(
            name=spec["name"],
            kind=spec["kind"],
            source_file=abs_source,
            # copy the mutable collections so the two rooted tables never alias.
            state_vars=[
                StateVarInfo(
                    sv.name, sv.type, sv.visibility, sv.is_constant,
                    sv.is_immutable, list(sv.writers),
                )
                for sv in spec["state_vars"]
            ],
            function_gates=[
                FunctionGate(
                    fg.name, fg.signature, fg.visibility, fg.mutability,
                    fg.modifier, fg.is_constructor, fg.is_fallback, fg.is_receive,
                )
                for fg in spec["function_gates"]
            ],
            caller_edges=[
                CallerEdge(
                    e.caller_contract, e.caller_function, e.callee_contract,
                    e.callee_function, e.call_type,
                )
                for e in spec["caller_edges"]
            ],
        )
        table.contracts[spec["name"]] = contract
    return table


def _rewrite_relative_to_root(payload: dict) -> dict:
    """Rewrite the payload's recorded absolute paths relative to its root.

    ``project_path`` is normalized to ``"."`` (the analyzed root relative to
    itself) and each contract's ``source_file`` is rewritten to its posix path
    relative to that root. This is the normalization the property requires: once
    the recorded paths are expressed relative to the analyzed root, the fields
    are location-independent.
    """
    rewritten = dict(payload)
    root = PurePosixPath(payload["project_path"])
    rewritten["project_path"] = "."

    new_contracts = {}
    for name, contract in payload["contracts"].items():
        c = dict(contract)
        src = PurePosixPath(contract["source_file"])
        c["source_file"] = str(src.relative_to(root))
        new_contracts[name] = c
    rewritten["contracts"] = new_contracts
    return rewritten


# ---------------------------------------------------------------------------
# Property 11 — path relocation metamorphic (R7.7)
# ---------------------------------------------------------------------------

# Two unrelated absolute roots. They share no common tail so the relocation is
# genuinely to an "unrelated" absolute path.
_ROOT_A = "/home/alice/projects/proj-a"
_ROOT_B = "/opt/build/checkout/xyz"


@settings(max_examples=200)
@given(_relocatable_table())
def test_relocation_yields_field_equal_artifacts_after_relativization(specs):
    """Relocating root A -> root B leaves the serialized Stage 1 fields equal
    once recorded absolute paths are rewritten relative to the analyzed root
    (Property 11, R7.7)."""
    table_a = _build_table(specs, _ROOT_A)
    table_b = _build_table(specs, _ROOT_B)

    payload_a = serialize_stage1(table_a)
    payload_b = serialize_stage1(table_b)

    # The two absolute-rooted payloads differ exactly at the path fields.
    if specs:
        assert payload_a != payload_b

    rewritten_a = _rewrite_relative_to_root(payload_a)
    rewritten_b = _rewrite_relative_to_root(payload_b)

    # After relativizing recorded paths to the analyzed root, every field is
    # equal: the artifact is invariant under relocation.
    assert rewritten_a == rewritten_b


@settings(max_examples=100)
@given(_relocatable_table())
def test_non_path_fields_are_identical_under_relocation(specs):
    """No field other than the two path-bearing fields (``project_path`` and
    each ``source_file``) may change under relocation; blanking those out must
    make the raw serialized payloads equal (R7.7)."""
    payload_a = serialize_stage1(_build_table(specs, _ROOT_A))
    payload_b = serialize_stage1(_build_table(specs, _ROOT_B))

    def _blank_paths(payload: dict) -> dict:
        out = dict(payload)
        out["project_path"] = ""
        out["contracts"] = {
            name: {**c, "source_file": ""}
            for name, c in payload["contracts"].items()
        }
        return out

    assert _blank_paths(payload_a) == _blank_paths(payload_b)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
