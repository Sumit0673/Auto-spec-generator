"""Unit tests for the Methods_Block_Generator (Requirement 17).

The module under test, ``spec_pipeline.methods_block``, is deliberately free of
any slither-tainted import chain, so it can be imported directly. To be doubly
safe against ``spec_pipeline/__init__.py`` eagerly importing the slither-backed
stages, we load the module by file path via importlib (mirroring
tests/unit/test_stage1_determinism.py) and fall back to a plain import.

Inputs are lightweight duck-typed objects rather than the real Stage 1
dataclasses, matching how the generator accesses the table (attribute access on
``table.contracts`` -> contract -> ``function_gates`` / ``state_vars``). This
keeps the tests independent of slither.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_methods_block():
    mod_name = "spec_pipeline_methods_block_under_test"
    path = _REPO_ROOT / "spec_pipeline" / "methods_block.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


try:  # prefer the real package path; fall back to direct-file load
    from spec_pipeline.methods_block import generate_methods_block  # type: ignore
    import spec_pipeline.methods_block as mb  # type: ignore
except Exception:  # pragma: no cover - slither absent
    mb = _load_methods_block()
    generate_methods_block = mb.generate_methods_block


# ---------------------------------------------------------------------------
# Duck-typed Stage 1 stand-ins
# ---------------------------------------------------------------------------


@dataclass
class SV:
    name: str
    type: str
    visibility: str = "public"
    is_constant: bool = False
    is_immutable: bool = False
    writers: list = field(default_factory=list)


@dataclass
class FG:
    name: str
    signature: str
    visibility: str = "external"
    mutability: str = "nonpayable"
    modifier: str = "none"
    is_constructor: bool = False
    is_fallback: bool = False
    is_receive: bool = False


@dataclass
class Contract:
    name: str
    state_vars: list = field(default_factory=list)
    function_gates: list = field(default_factory=list)


@dataclass
class Table:
    contracts: dict = field(default_factory=dict)
    project_path: str = "/proj"


def _table(*contracts: Contract) -> Table:
    return Table(contracts={c.name: c for c in contracts})


# ---------------------------------------------------------------------------
# R17.1 / R17.2 — getter uses sv.type and emits the return type
# ---------------------------------------------------------------------------


def test_getter_reads_sv_type_and_emits_return_type():
    c = Contract("Token", state_vars=[SV("totalSupply", "uint256")])
    res = generate_methods_block(_table(c))
    assert "function totalSupply() external returns (uint256) envfree;" in res.text
    # The dot getter is envfree per R17.7.
    assert "envfree" in res.text


def test_getter_return_type_not_defaulted_to_uint256():
    c = Contract("Token", state_vars=[SV("owner", "address")])
    res = generate_methods_block(_table(c))
    assert "returns (address)" in res.text
    assert "returns (uint256)" not in res.text


# ---------------------------------------------------------------------------
# R17.11 — mapping / array getter parameters + innermost value as return
# ---------------------------------------------------------------------------


def test_mapping_getter_one_param_per_key_level():
    c = Contract(
        "Token",
        state_vars=[SV("balanceOf", "mapping(address => uint256)")],
    )
    res = generate_methods_block(_table(c))
    assert "function balanceOf(address) external returns (uint256) envfree;" in res.text


def test_nested_mapping_getter_multiple_params():
    c = Contract(
        "Token",
        state_vars=[SV("allowance", "mapping(address => mapping(address => uint256))")],
    )
    res = generate_methods_block(_table(c))
    assert (
        "function allowance(address, address) external returns (uint256) envfree;"
        in res.text
    )


def test_array_getter_index_param():
    c = Contract("Token", state_vars=[SV("holders", "address[]")])
    res = generate_methods_block(_table(c))
    assert "function holders(uint256) external returns (address) envfree;" in res.text


# ---------------------------------------------------------------------------
# R17.3 — two contracts sharing a function name keep both entries
# ---------------------------------------------------------------------------


def test_two_contracts_sharing_function_name_keep_both():
    a = Contract("Alpha", function_gates=[FG("pause", "() -> bool", mutability="view")])
    b = Contract("Beta", function_gates=[FG("pause", "() -> bool", mutability="view")])
    res = generate_methods_block(_table(a, b))
    names = [e.name for e in res.entries]
    assert names.count("pause") == 2
    contracts = {e.contract for e in res.entries if e.name == "pause"}
    assert contracts == {"Alpha", "Beta"}


def test_same_signature_within_one_contract_deduped():
    c = Contract(
        "Alpha",
        function_gates=[
            FG("foo", "(uint256 a) -> bool", mutability="view"),
            FG("foo", "(uint256 b) -> bool", mutability="view"),
        ],
    )
    res = generate_methods_block(_table(c))
    assert [e.name for e in res.entries].count("foo") == 1


# ---------------------------------------------------------------------------
# R17.4 — multi-contract using/alias qualification
# ---------------------------------------------------------------------------


def test_multi_contract_using_and_alias_prefix():
    a = Contract("Alpha", function_gates=[FG("a", "() -> bool", mutability="view")])
    b = Contract("Beta", function_gates=[FG("b", "() -> bool", mutability="view")])
    res = generate_methods_block(_table(a, b))
    # First (sorted) contract is unqualified; the rest get `using`.
    assert "using Beta as Beta;" in res.text
    assert "using Alpha" not in res.text
    # Beta's entry is alias-qualified; Alpha's is not.
    assert "function Beta.b() external" in res.text
    assert "function a() external" in res.text


def test_single_contract_has_no_using():
    a = Contract("Alpha", function_gates=[FG("a", "() -> bool", mutability="view")])
    res = generate_methods_block(_table(a))
    assert "using " not in res.text


# ---------------------------------------------------------------------------
# R17.5 — CVL-expressible struct emits a type declaration and keeps the entry
# ---------------------------------------------------------------------------


def test_cvl_expressible_struct_emits_declaration_and_keeps_entry():
    source = """
    struct Point { uint256 x; address owner; }
    """
    c = Contract(
        "Geo",
        function_gates=[FG("move", "(Point p) -> bool", mutability="nonpayable")],
    )
    res = generate_methods_block(_table(c), source_code=source)
    assert "struct Point {" in res.text
    assert "uint256 x;" in res.text
    assert "address owner;" in res.text
    # The function is kept as an entry (not excluded).
    assert any(e.name == "move" for e in res.entries)
    assert not any(x.name == "move" for x in res.exclusion_report)


# ---------------------------------------------------------------------------
# R17.6 / R17.12 — unsupported type omitted + in exclusion report, no uint256
# ---------------------------------------------------------------------------


def test_unsupported_struct_omitted_and_reported():
    # A struct with a mapping field has no CVL expression.
    source = """
    struct Outer { mapping(address => uint256) balances; uint256 n; }
    """
    c = Contract(
        "Cfg",
        function_gates=[FG("set", "(Outer o) -> bool", mutability="nonpayable")],
    )
    res = generate_methods_block(_table(c), source_code=source)
    # Function omitted from entries.
    assert not any(e.name == "set" for e in res.entries)
    # Recorded in the exclusion report with reason unsupported_type.
    excl = [x for x in res.exclusion_report if x.name == "set"]
    assert len(excl) == 1
    assert excl[0].reason == "unsupported_type"
    assert excl[0].contract == "Cfg"
    assert excl[0].type  # offending type text present
    # No silent uint256 substitution for the omitted function.
    assert "function set(" not in res.text


def test_unsupported_getter_type_omitted_and_reported():
    # A struct with a mapping field cannot be expressed in CVL.
    source = """
    struct Blob { mapping(address => uint256) m; }
    """
    c = Contract("Store", state_vars=[SV("blob", "Blob")])
    res = generate_methods_block(_table(c), source_code=source)
    assert not any(e.name == "blob" for e in res.entries)
    excl = [x for x in res.exclusion_report if x.name == "blob"]
    assert len(excl) == 1
    assert excl[0].reason == "unsupported_type"


# ---------------------------------------------------------------------------
# R17.7 — envfree only for view/pure gates; getters always envfree
# ---------------------------------------------------------------------------


def test_envfree_only_for_view_or_pure():
    c = Contract(
        "Token",
        function_gates=[
            FG("readIt", "() -> uint256", mutability="view"),
            FG("pureIt", "() -> uint256", mutability="pure"),
            FG("writeIt", "(uint256 x)", mutability="nonpayable"),
            FG("payIt", "(uint256 x)", mutability="payable"),
        ],
    )
    res = generate_methods_block(_table(c))
    by_name = {e.name: e for e in res.entries}
    assert by_name["readIt"].envfree is True
    assert by_name["pureIt"].envfree is True
    assert by_name["writeIt"].envfree is False
    assert by_name["payIt"].envfree is False


def test_getter_always_envfree():
    c = Contract("Token", state_vars=[SV("x", "uint256")])
    res = generate_methods_block(_table(c))
    assert res.entries[0].is_getter is True
    assert res.entries[0].envfree is True


# ---------------------------------------------------------------------------
# R17.8 — ordering by contract, function, param types
# ---------------------------------------------------------------------------


def test_entries_ordered_by_contract_then_function_then_params():
    a = Contract(
        "Zeta",
        function_gates=[
            FG("b", "() -> bool", mutability="view"),
            FG("a", "(uint256 x) -> bool", mutability="view"),
            FG("a", "(address y) -> bool", mutability="view"),
        ],
    )
    b = Contract("Alpha", function_gates=[FG("z", "() -> bool", mutability="view")])
    res = generate_methods_block(_table(a, b))
    keys = [(e.contract, e.name, e.param_types) for e in res.entries]
    assert keys == sorted(keys)
    # Alpha comes before Zeta.
    assert keys[0][0] == "Alpha"


# ---------------------------------------------------------------------------
# R17.10 — idempotence
# ---------------------------------------------------------------------------


def test_idempotent_text():
    source = "struct Point { uint256 x; address o; }"
    a = Contract(
        "Alpha",
        state_vars=[SV("bal", "mapping(address => uint256)"), SV("owner", "address")],
        function_gates=[
            FG("move", "(Point p) -> bool", mutability="nonpayable"),
            FG("view1", "() -> uint256", mutability="view"),
        ],
    )
    b = Contract("Beta", function_gates=[FG("ping", "() -> bool", mutability="view")])
    t = _table(a, b)
    first = generate_methods_block(t, source_code=source).text
    second = generate_methods_block(t, source_code=source).text
    assert first == second


# ---------------------------------------------------------------------------
# R17.9 — completeness invariant: every gate is an entry XOR in exclusions
# ---------------------------------------------------------------------------


def test_completeness_invariant_every_gate_entry_xor_excluded():
    source = """
    struct Bad { mapping(uint256 => uint256) m; }
    struct Good { uint256 a; address b; }
    """
    a = Contract(
        "Alpha",
        function_gates=[
            FG("ok", "(uint256 x) -> bool", mutability="view"),
            FG("bad", "(Bad b)", mutability="nonpayable"),
            FG("good", "(Good g)", mutability="nonpayable"),
            FG("ctor", "()", is_constructor=True),  # special: neither
        ],
    )
    res = generate_methods_block(_table(a), source_code=source)

    entry_gate_names = {e.name for e in res.entries if not e.is_getter}
    excluded_names = {x.name for x in res.exclusion_report}

    non_special = ["ok", "bad", "good"]
    for name in non_special:
        in_entry = name in entry_gate_names
        in_excl = name in excluded_names
        # exactly one of the two
        assert in_entry ^ in_excl, f"{name}: entry={in_entry} excl={in_excl}"

    # The constructor appears in neither.
    assert "ctor" not in entry_gate_names
    assert "ctor" not in excluded_names

    # Every emitted entry maps to a known gate or state var.
    known = {"ok", "bad", "good", "good", "ctor"}
    for e in res.entries:
        assert e.name in {"ok", "good"} or e.is_getter


def test_empty_table_produces_empty_methods_block():
    res = generate_methods_block(_table())
    assert res.text.strip() == "methods {\n}".strip() or res.text.strip().endswith("}")
    assert res.entries == []
    assert res.exclusion_report == []


def test_result_str_is_text():
    c = Contract("Token", state_vars=[SV("x", "uint256")])
    res = generate_methods_block(_table(c))
    assert str(res) == res.text
    # exclusions convenience view
    assert isinstance(res.exclusions, list)


# ---------------------------------------------------------------------------
# BUG 1 — contract/interface-typed getters and params resolve to address
# ---------------------------------------------------------------------------


def test_contract_typed_getter_resolves_to_address():
    # A public state var typed as another contract/interface in the table is a
    # reference type -> CVL address; the getter must be emitted, not excluded.
    # The referenced interface is in the table as a contract (the real Stage 1
    # output lists CompInterface/TimelockInterface as contracts). Governor
    # sorts after CompInterface, so its entries are alias-qualified.
    gov = Contract("Governor", state_vars=[SV("comp", "CompInterface")])
    comp = Contract("CompInterface")
    res = generate_methods_block(_table(gov, comp))
    assert (
        "function Governor.comp() external returns (address) envfree;" in res.text
    )
    assert not any(x.name == "comp" for x in res.exclusion_report)
    entry = next(e for e in res.entries if e.name == "comp")
    assert entry.return_type == "address"


def test_array_of_contract_type_resolves_to_address_array():
    # An array of a contract type must resolve to address[] via the recursive
    # array path. A public array getter peels index dimensions (so it returns
    # the element type), therefore exercise the array through a function that
    # takes/returns the array directly.
    gov = Contract(
        "Governor",
        function_gates=[
            FG(
                "listDelegates",
                "(CompInterface[] xs) -> CompInterface[]",
                mutability="view",
            )
        ],
    )
    comp = Contract("CompInterface")
    res = generate_methods_block(_table(gov, comp))
    entry = next(e for e in res.entries if e.name == "listDelegates")
    assert entry.param_types == ("address[]",)
    assert entry.return_type == "address[]"
    assert "function Governor.listDelegates(address[]) external returns (address[])" in res.text
    assert not any(x.name == "listDelegates" for x in res.exclusion_report)


def test_contract_typed_param_resolves_to_address():
    gov = Contract(
        "Governor",
        function_gates=[
            FG("setComp", "(CompInterface newComp)", mutability="nonpayable")
        ],
    )
    comp = Contract("CompInterface")
    res = generate_methods_block(_table(gov, comp))
    entry = next(e for e in res.entries if e.name == "setComp")
    assert entry.param_types == ("address",)
    assert "function Governor.setComp(address) external" in res.text
    assert not any(x.name == "setComp" for x in res.exclusion_report)


# ---------------------------------------------------------------------------
# BUG 2 — multi-value return types resolve to returns (A, B, C)
# ---------------------------------------------------------------------------


def test_multi_value_return_resolves_all_components():
    c = Contract(
        "Gov",
        function_gates=[
            FG(
                "proposalVotes",
                "(uint256 proposalId) -> uint256, uint256, uint256",
                mutability="view",
            )
        ],
    )
    res = generate_methods_block(_table(c))
    assert (
        "function proposalVotes(uint256) external "
        "returns (uint256, uint256, uint256) envfree;"
    ) in res.text
    # not double-wrapped
    assert "returns ((" not in res.text
    assert not any(x.name == "proposalVotes" for x in res.exclusion_report)
    entry = next(e for e in res.entries if e.name == "proposalVotes")
    assert entry.return_type == "uint256, uint256, uint256"


def test_multi_value_return_with_unsupported_component_excluded():
    # A struct with a mapping field is not CVL-expressible.
    source = "struct Bad { mapping(address => uint256) m; }"
    c = Contract(
        "Gov",
        function_gates=[
            FG("mix", "(uint256 x) -> uint256, Bad", mutability="view")
        ],
    )
    res = generate_methods_block(_table(c), source_code=source)
    assert not any(e.name == "mix" for e in res.entries)
    excl = [x for x in res.exclusion_report if x.name == "mix"]
    assert len(excl) == 1
    assert excl[0].reason == "unsupported_type"
    assert excl[0].type == "Bad"


def test_multi_value_return_with_contract_type_component():
    gov = Contract(
        "Gov",
        function_gates=[
            FG("both", "() -> uint256, CompInterface", mutability="view")
        ],
    )
    comp = Contract("CompInterface")
    res = generate_methods_block(_table(gov, comp))
    entry = next(e for e in res.entries if e.name == "both")
    assert entry.return_type == "uint256, address"
    assert "returns (uint256, address)" in res.text
    assert not any(x.name == "both" for x in res.exclusion_report)


# ---------------------------------------------------------------------------
# Regression — an unsupported struct getter is STILL excluded (unchanged)
# ---------------------------------------------------------------------------


def test_unsupported_struct_getter_still_excluded_regression():
    source = "struct Blob { mapping(address => uint256) m; }"
    c = Contract("Store", state_vars=[SV("blob", "Blob")])
    res = generate_methods_block(_table(c), source_code=source)
    assert not any(e.name == "blob" for e in res.entries)
    excl = [x for x in res.exclusion_report if x.name == "blob"]
    assert len(excl) == 1
    assert excl[0].reason == "unsupported_type"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
