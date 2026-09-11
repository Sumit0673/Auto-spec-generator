"""Unit tests for the Context_Budgeter (Requirement 16).

``spec_pipeline.context`` is slither-free, but the ``spec_pipeline`` package
``__init__`` eagerly imports slither. We therefore load the module BY FILE PATH
via importlib (mirroring tests/unit/test_prompts_lint.py) so the suite runs with
slither absent.

Covers:
- R16.1/R16.10: budget from LLM_MAX_INPUT_CHARS; invalid value -> 200000 default
  and the rejected value is recorded.
- R16.2/R16.3: oversize selection follows priority (analyzed contract, then
  caller-reachable by hop, then rest) and cuts at .sol file boundaries.
- R16.4: omissions recorded (paths + count) and omitted contract names surfaced.
- R16.5: in-budget source is included in full.
- R16.6/R16.11: substitution when the analyzed contract alone exceeds budget.
- R16.7/R16.8: merge helper renames colliding rule names and records renames.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_by_path(mod_name: str, rel_path: str) -> ModuleType:
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ctx() -> ModuleType:
    try:
        import spec_pipeline.context as c  # type: ignore

        return c
    except Exception:  # pragma: no cover - slither absent
        return _load_by_path("context_under_test", "spec_pipeline/context.py")


# ---------------------------------------------------------------------------
# Duck-typed Stage 1 stand-ins
# ---------------------------------------------------------------------------


@dataclass
class FG:
    name: str
    signature: str = "()"
    visibility: str = "external"
    mutability: str = "nonpayable"
    modifier: str = "none"
    is_constructor: bool = False
    is_fallback: bool = False
    is_receive: bool = False


@dataclass
class Edge:
    caller_contract: str
    caller_function: str
    callee_contract: str
    callee_function: str
    call_type: str = "external"


@dataclass
class Contract:
    name: str
    source_file: str = ""
    function_gates: list = field(default_factory=list)
    caller_edges: list = field(default_factory=list)


@dataclass
class Table:
    contracts: dict = field(default_factory=dict)


def _sf(ctx, path, content, contracts=()):
    return ctx.SourceFile(path=path, content=content, contracts=tuple(contracts))


# ===========================================================================
# R16.1 / R16.10 — budget resolution
# ===========================================================================


def test_default_budget_when_env_absent(ctx):
    res = ctx.resolve_budget({})
    assert res.budget == 200000
    assert res.rejected_value is None


def test_budget_reads_positive_integer(ctx):
    res = ctx.resolve_budget({"LLM_MAX_INPUT_CHARS": "1234"})
    assert res.budget == 1234
    assert res.rejected_value is None


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "12.5", "", "  "])
def test_invalid_budget_falls_back_and_records_rejected(ctx, bad):
    res = ctx.resolve_budget({"LLM_MAX_INPUT_CHARS": bad})
    assert res.budget == 200000
    assert res.rejected_value == bad

    # And the same surfaces through the budgeter + PackResult.
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": bad})
    assert b.budget == 200000
    r = b.pack_source(raw_source="contract A {}")
    assert r.rejected_budget_value == bad


# ===========================================================================
# R16.5 — in-budget source included in full
# ===========================================================================


def test_in_budget_raw_source_included_in_full(ctx):
    b = ctx.Context_Budgeter(env={})
    src = "contract A { uint x; }"
    r = b.pack_source(raw_source=src)
    assert r.text == src
    assert r.omission.is_empty
    assert r.omitted_contract_names == []


def test_in_budget_files_all_included(ctx):
    b = ctx.Context_Budgeter(env={})
    files = [
        _sf(ctx, "A.sol", "contract A {}", ["A"]),
        _sf(ctx, "B.sol", "contract B {}", ["B"]),
    ]
    r = b.pack_source(files=files, analyzed_contract="A")
    assert "contract A {}" in r.text and "contract B {}" in r.text
    assert r.omission.is_empty


# ===========================================================================
# R16.2 / R16.3 / R16.4 — oversize selection, boundaries, omission record
# ===========================================================================


def test_oversize_priority_and_boundary_cut(ctx):
    # Budget fits the analyzed file + one reachable file, not the third.
    analyzed = _sf(ctx, "A.sol", "A" * 100, ["A"])
    callee = _sf(ctx, "B.sol", "B" * 100, ["B"])
    other = _sf(ctx, "C.sol", "C" * 100, ["C"])

    table = Table(
        contracts={
            "A": Contract(
                "A",
                "A.sol",
                caller_edges=[Edge("A", "f", "B", "g")],
            ),
            "B": Contract("B", "B.sol"),
            "C": Contract("C", "C.sol"),
        }
    )
    # separator is "\n\n" (2 chars): 100 + 2 + 100 = 202 fits; +2+100 would not.
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "205"})
    r = b.pack_source(files=[other, callee, analyzed], analyzed_contract="A", table=table)

    # Analyzed A first, reachable B second, C omitted (boundary cut).
    assert r.text.index("A" * 100) < r.text.index("B" * 100)
    assert "C" * 100 not in r.text
    assert r.omission.omitted_paths == ["C.sol"]
    assert r.omission.omitted_char_count == 100
    assert r.omitted_contract_names == ["C"]


def test_reachable_ordered_by_hop_distance(ctx):
    # A -> B -> C. With a tight budget only A and B fit; C (hop 2) is omitted
    # before D (unreachable) regardless of path order.
    a = _sf(ctx, "a.sol", "A" * 50, ["A"])
    b_ = _sf(ctx, "b.sol", "B" * 50, ["B"])
    c = _sf(ctx, "c.sol", "C" * 50, ["C"])
    d = _sf(ctx, "d.sol", "D" * 50, ["D"])
    table = Table(
        contracts={
            "A": Contract("A", "a.sol", caller_edges=[Edge("A", "f", "B", "g")]),
            "B": Contract("B", "b.sol", caller_edges=[Edge("B", "g", "C", "h")]),
            "C": Contract("C", "c.sol"),
            "D": Contract("D", "d.sol"),
        }
    )
    # 50 + 2 + 50 = 102 fits two files only.
    bud = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "103"})
    r = bud.pack_source(files=[d, c, b_, a], analyzed_contract="A", table=table)
    assert "A" * 50 in r.text and "B" * 50 in r.text
    assert "C" * 50 not in r.text and "D" * 50 not in r.text
    # Both C and D omitted; names surfaced sorted.
    assert r.omitted_contract_names == ["C", "D"]


def test_equal_priority_ordered_by_relative_path(ctx):
    # Two unreachable files, both tier 2 -> ordered by ascending path.
    z = _sf(ctx, "z.sol", "Z" * 10, ["Z"])
    a = _sf(ctx, "a.sol", "A" * 10, ["A"])
    b = ctx.Context_Budgeter(env={})
    r = b.pack_source(files=[z, a])
    assert r.text.index("A" * 10) < r.text.index("Z" * 10)


# ===========================================================================
# R16.6 / R16.11 — substitution when analyzed contract alone exceeds budget
# ===========================================================================


def test_substitution_uses_gated_function_bodies(ctx):
    body = "function pull(uint256 amt) external onlyOwner { balance -= amt; }"
    big = "contract Vault {\n" + body + "\n" + ("//pad\n" * 500) + "}"
    analyzed = _sf(ctx, "Vault.sol", big, ["Vault"])
    table = Table(
        contracts={
            "Vault": Contract(
                "Vault",
                "Vault.sol",
                function_gates=[FG("pull", "(uint256 amt)", modifier="onlyOwner")],
            )
        }
    )
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "600"})
    r = b.pack_source(
        files=[analyzed],
        analyzed_contract="Vault",
        table=table,
        table_entry_text="=== Contract: Vault ===",
    )
    assert r.omission.substituted_contract_body is True
    assert r.omission.substituted_signatures_only is False
    assert "function pull" in r.text
    assert "=== Contract: Vault ===" in r.text
    assert "Vault.sol" in r.omission.omitted_paths
    assert r.omission.omitted_char_count == len(big)


def test_substitution_falls_back_to_signatures_when_bodies_too_big(ctx):
    huge_body = "function pull(uint256 amt) external onlyOwner {" + ("x" * 5000) + "}"
    big = "contract Vault {\n" + huge_body + "\n}"
    analyzed = _sf(ctx, "Vault.sol", big, ["Vault"])
    table = Table(
        contracts={
            "Vault": Contract(
                "Vault",
                "Vault.sol",
                function_gates=[FG("pull", "(uint256 amt)", modifier="onlyOwner")],
            )
        }
    )
    # Budget too small for the 5000-char body but fine for signatures.
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "400"})
    r = b.pack_source(
        files=[analyzed],
        analyzed_contract="Vault",
        table=table,
        table_entry_text="=== Contract: Vault ===",
    )
    assert r.omission.substituted_contract_body is True
    assert r.omission.substituted_signatures_only is True
    assert "function pull(uint256 amt)" in r.text
    assert "x" * 5000 not in r.text


# ===========================================================================
# R16.9 fallback — raw source too big is omitted, never truncated
# ===========================================================================


def test_raw_source_over_budget_recorded_not_truncated(ctx):
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "10"})
    src = "contract A {" + ("x" * 100) + "}"
    r = b.pack_source(raw_source=src)
    assert r.text == ""  # never a partial slice
    assert r.omission.omitted_paths == ["<raw_source>"]
    assert r.omission.omitted_char_count == len(src)


# ===========================================================================
# R16.7 / R16.8 — merge helper renames colliding rule names + records renames
# ===========================================================================


def test_merge_single_methods_block_and_no_collision(ctx):
    a = "rule foo() { assert true; }"
    b = "rule bar() { assert true; }"
    res = ctx.merge_per_contract_specs(
        [("A", a), ("B", b)], methods_block="methods { f() external; }"
    )
    assert res.spec_text.count("methods {") == 1
    assert res.renames == []
    assert "rule foo" in res.spec_text and "rule bar" in res.spec_text


def test_merge_renames_colliding_rules_and_records(ctx):
    a = "rule mono() { assert true; }"
    b = "rule mono() { assert false; }"
    res = ctx.merge_per_contract_specs([("A", a), ("B", b)])
    # First keeps original; second renamed with the contract name appended.
    assert "rule mono" in res.spec_text
    assert "rule mono_B" in res.spec_text
    assert len(res.renames) == 1
    r = res.renames[0]
    assert (r.original, r.renamed, r.contract) == ("mono", "mono_B", "B")


def test_merge_ascending_integer_on_further_collision(ctx):
    # Three contracts all declare rule mono; the third would collide with
    # mono_C only if a name clashed, so exercise the same-name-across-many path.
    a = "rule mono() {}"
    b = "rule mono() {}"
    c = "rule mono() {}"
    res = ctx.merge_per_contract_specs([("X", a), ("X", b), ("X", c)])
    # All contract names are X -> mono, mono_X, mono_X_2.
    assert "rule mono_X" in res.spec_text
    assert "rule mono_X_2" in res.spec_text
    assert [(r.original, r.renamed) for r in res.renames] == [
        ("mono", "mono_X"),
        ("mono", "mono_X_2"),
    ]


def test_merge_strips_embedded_methods_blocks(ctx):
    a = "methods { g() external; }\nrule foo() {}"
    res = ctx.merge_per_contract_specs(
        [("A", a)], methods_block="methods { f() external; }"
    )
    # Only the provided methods block remains.
    assert res.spec_text.count("methods {") == 1
    assert "f() external;" in res.spec_text
    assert "g() external;" not in res.spec_text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
