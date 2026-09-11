"""Unit tests for Context_Budgeter budgeting behavior (Task 12.5).

Validates Requirements 16.4, 16.5, 16.6, 16.8.

Where ``tests/unit/test_context.py`` exercises the budgeter's data-level API,
this module asserts the *behavior* task 12.5 calls out, end to end:

- R16.5: an in-budget source is included in full.
- R16.2/R16.3 (feeding R16.4): oversize selection follows the priority order
  (contract under analysis -> callee-reachable -> rest) and cuts at ``.sol``
  file boundaries (whole files only, never a partial slice).
- R16.4: omissions are RECORDED as the task states them -- omitted paths and
  the omitted character count surface in the Run_Manifest dict
  (``OmissionRecord.to_manifest``) AND the omitted contract names are named in
  the assembled prompt (``prompts.format_stage3_user`` / ``_omission_notice``).
- R16.6: when the analyzed contract's own source exceeds the budget, the
  substitution (Stage 1 entry + gated-function bodies) is recorded in the
  manifest dict.
- R16.8: colliding merged rule names are renamed AND each rename is recorded.

Both ``spec_pipeline.context`` and ``spec_pipeline.prompts`` are slither-free,
but the ``spec_pipeline`` package ``__init__`` eagerly imports slither. We
therefore load the modules BY FILE PATH via importlib (mirroring
tests/unit/test_context.py and tests/unit/test_prompts_lint.py), so the suite
runs offline with slither/solc/certoraRun absent.
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
        return _load_by_path("context_budgeting_uut", "spec_pipeline/context.py")


@pytest.fixture(scope="module")
def prompts() -> ModuleType:
    try:
        import spec_pipeline.prompts as p  # type: ignore

        return p
    except Exception:  # pragma: no cover - slither absent
        return _load_by_path("prompts_budgeting_uut", "spec_pipeline/prompts.py")


# ---------------------------------------------------------------------------
# Duck-typed Stage 1 stand-ins (mirror the real dataclass shape by attribute)
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
# R16.5 — in-budget source is included in full
# ===========================================================================


def test_in_budget_files_included_in_full_no_omission(ctx):
    """Every file fits: all content present, and the manifest shows nothing dropped."""
    b = ctx.Context_Budgeter(env={})  # default 200000 budget
    files = [
        _sf(ctx, "A.sol", "contract A { uint256 x; }", ["A"]),
        _sf(ctx, "B.sol", "contract B { uint256 y; }", ["B"]),
    ]
    r = b.pack_source(files=files, analyzed_contract="A")

    assert "contract A { uint256 x; }" in r.text
    assert "contract B { uint256 y; }" in r.text
    assert r.omission.is_empty
    assert r.omitted_contract_names == []
    manifest = r.omission.to_manifest()
    assert manifest["omitted_paths"] == []
    assert manifest["omitted_char_count"] == 0
    assert manifest["substituted_contract_body"] is False
    assert manifest["substituted_signatures_only"] is False


def test_in_budget_analyzed_source_included_whole(ctx):
    """R16.5: the analyzed contract source fits and is emitted verbatim."""
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "100000"})
    src = "contract Vault {\n  uint256 total;\n  function dep() external {}\n}"
    analyzed = _sf(ctx, "Vault.sol", src, ["Vault"])
    r = b.pack_source(files=[analyzed], analyzed_contract="Vault")
    assert r.text == src
    assert r.omission.is_empty


# ===========================================================================
# R16.2 / R16.3 / R16.4 — oversize priority, boundary cut, recorded omission
# ===========================================================================


def test_oversize_priority_order_and_file_boundary_cut(ctx):
    """Analyzed contract first, then callee-reachable, rest omitted whole (R16.2/16.3)."""
    analyzed = _sf(ctx, "A.sol", "A" * 100, ["A"])
    callee = _sf(ctx, "B.sol", "B" * 100, ["B"])
    rest = _sf(ctx, "C.sol", "C" * 100, ["C"])
    table = Table(
        contracts={
            "A": Contract("A", "A.sol", caller_edges=[Edge("A", "f", "B", "g")]),
            "B": Contract("B", "B.sol"),
            "C": Contract("C", "C.sol"),
        }
    )
    # separator "\n\n" (2 chars): 100 + 2 + 100 = 202 fits; a third file would not.
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "205"})
    # Deliberately shuffle input order to prove ordering comes from priority.
    r = b.pack_source(files=[rest, callee, analyzed], analyzed_contract="A", table=table)

    # Priority: analyzed A precedes reachable B in the packed text.
    assert r.text.index("A" * 100) < r.text.index("B" * 100)
    # C is cut at the file boundary -- included/omitted whole, never partially.
    assert "C" not in r.text
    # Cut is at a boundary: exactly the two whole files, joined by the separator.
    assert r.text == ("A" * 100) + "\n\n" + ("B" * 100)
    # And C's omission is recorded for the Run_Manifest.
    assert r.omission.to_manifest()["omitted_paths"] == ["C.sol"]
    assert r.omission.to_manifest()["omitted_char_count"] == 100
    assert r.omitted_contract_names == ["C"]


def test_omission_recorded_paths_and_char_count_in_manifest(ctx):
    """R16.4: omitted paths + char count are recorded for the Run_Manifest."""
    analyzed = _sf(ctx, "A.sol", "A" * 100, ["A"])
    rest1 = _sf(ctx, "B.sol", "B" * 100, ["B"])
    rest2 = _sf(ctx, "C.sol", "C" * 60, ["C"])
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "100"})  # only A fits
    r = b.pack_source(files=[analyzed, rest1, rest2], analyzed_contract="A")

    manifest = r.omission.to_manifest()
    assert manifest["omitted_paths"] == ["B.sol", "C.sol"]  # sorted
    assert manifest["omitted_char_count"] == 160  # 100 + 60
    assert not r.omission.is_empty


def test_omitted_contract_names_named_in_prompt(ctx, prompts, monkeypatch):
    """R16.4: omitted contract names are stated in the assembled Stage 3 prompt.

    ``format_stage3_user`` resolves the budget from the process environment, so
    we set a tight ``LLM_MAX_INPUT_CHARS`` to force the reachable-but-oversize
    file to be omitted and the notice to fire.
    """
    monkeypatch.setenv("LLM_MAX_INPUT_CHARS", "150")  # only the ~120-char analyzed file fits
    analyzed = _sf(ctx, "A.sol", "A" * 120, ["A"])
    dropped = _sf(ctx, "Zeta.sol", "Z" * 120, ["Zeta"])
    table = Table(contracts={"A": Contract("A", "A.sol"), "Zeta": Contract("Zeta", "Zeta.sol")})

    prompt = prompts.format_stage3_user(
        stage1_table_text="=== Stage 1 table ===",
        stage2_invariants={},
        source_code="A" * 120,  # raw fallback ignored when files provided
        methods_block="methods { }",
        files=[analyzed, dropped],
        analyzed_contract="A",
        table=table,
    )

    # The omitted-context notice fires and names the dropped contract (R16.4).
    assert "OMITTED CONTEXT" in prompt
    assert "Zeta" in prompt
    # The analyzed contract's content is present; the dropped file's is not.
    assert "A" * 120 in prompt
    assert "Z" * 120 not in prompt


def test_no_omission_notice_when_everything_fits(ctx, prompts):
    """R16.4 (negative): no omission -> no omitted-context notice in the prompt."""
    files = [_sf(ctx, "A.sol", "contract A {}", ["A"])]
    prompt = prompts.format_stage3_user(
        stage1_table_text="=== Stage 1 table ===",
        stage2_invariants={},
        source_code="contract A {}",
        methods_block="methods { }",
        files=files,
        analyzed_contract="A",
        table=Table(contracts={"A": Contract("A", "A.sol")}),
    )
    assert "OMITTED CONTEXT" not in prompt


# ===========================================================================
# R16.6 — substitution recorded when the analyzed contract alone is oversize
# ===========================================================================


def test_oversize_analyzed_contract_substitution_recorded(ctx):
    """R16.6: analyzed source > budget -> substitute entry + gated bodies, recorded."""
    body = "function pull(uint256 amt) external onlyOwner { balance -= amt; }"
    big = "contract Vault {\n" + body + "\n" + ("// pad line\n" * 400) + "}"
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
    b = ctx.Context_Budgeter(env={"LLM_MAX_INPUT_CHARS": "700"})
    r = b.pack_source(
        files=[analyzed],
        analyzed_contract="Vault",
        table=table,
        table_entry_text="=== Contract: Vault ===",
    )

    # The gated-function body is substituted in place of the oversize source.
    assert "function pull" in r.text
    assert "=== Contract: Vault ===" in r.text
    # Substitution recorded in the Run_Manifest dict (R16.6).
    manifest = r.omission.to_manifest()
    assert manifest["substituted_contract_body"] is True
    assert manifest["substituted_signatures_only"] is False
    assert "Vault.sol" in manifest["omitted_paths"]
    assert manifest["omitted_char_count"] == len(big)


# ===========================================================================
# R16.8 — colliding merged rule names renamed AND recorded
# ===========================================================================


def test_merged_colliding_rule_names_renamed_and_recorded(ctx):
    """R16.8: a colliding rule name is renamed with the contract and recorded."""
    per_contract = [
        ("Vault", "rule solvency() { assert true; }"),
        ("Pool", "rule solvency() { assert false; }"),
    ]
    res = ctx.merge_per_contract_specs(per_contract, methods_block="methods { f(); }")

    # First occurrence keeps the original name; the collision is renamed.
    assert "rule solvency" in res.spec_text
    assert "rule solvency_Pool" in res.spec_text
    # Exactly one methods block survives the merge.
    assert res.spec_text.count("methods {") == 1
    # The rename is recorded with original, renamed, and contract.
    assert len(res.renames) == 1
    rename = res.renames[0]
    assert (rename.original, rename.renamed, rename.contract) == (
        "solvency",
        "solvency_Pool",
        "Pool",
    )


def test_merged_repeated_collision_uses_ascending_integer(ctx):
    """R16.8: further collisions on the same name get an ascending integer suffix."""
    per_contract = [
        ("C", "rule inv() {}"),
        ("C", "rule inv() {}"),
        ("C", "rule inv() {}"),
    ]
    res = ctx.merge_per_contract_specs(per_contract)
    assert "rule inv_C" in res.spec_text
    assert "rule inv_C_2" in res.spec_text
    assert [(r.original, r.renamed) for r in res.renames] == [
        ("inv", "inv_C"),
        ("inv", "inv_C_2"),
    ]


def test_merge_without_collisions_records_no_renames(ctx):
    """R16.8 (negative): distinct rule names merge with an empty rename log."""
    per_contract = [
        ("A", "rule alpha() {}"),
        ("B", "rule beta() {}"),
    ]
    res = ctx.merge_per_contract_specs(per_contract)
    assert res.renames == []
    assert "rule alpha" in res.spec_text and "rule beta" in res.spec_text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
