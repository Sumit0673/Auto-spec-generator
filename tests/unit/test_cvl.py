"""Unit tests for the CVL_Extractor + CVL_Validator (Requirement 18).

The module under test, ``spec_pipeline.cvl``, is pure regex/text handling with
no slither-tainted import chain. To stay robust against
``spec_pipeline/__init__.py`` eagerly importing the slither-backed stages, we
load the module by file path via importlib (mirroring
tests/unit/test_methods_block.py) and fall back to a plain import.

These tests are deliberately anti-regression heavy: the deleted destructive
rewrites (``<= -> < +1``, ``!= 0 -> > 0``, ``!= address(0) -> > address(0)``,
``invariant`` deletion, ``using`` stripping) MUST NOT reappear, so we assert
the relevant fragments survive extraction unchanged (R18.5).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_cvl():
    mod_name = "spec_pipeline_cvl_under_test"
    path = _REPO_ROOT / "spec_pipeline" / "cvl.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


try:  # prefer the real package path; fall back to direct-file load
    from spec_pipeline import cvl as _cvl  # type: ignore
except Exception:  # pragma: no cover - slither absent
    _cvl = _load_cvl()

extract_cvl = _cvl.extract_cvl
validate_cvl = _cvl.validate_cvl
autofix_cvl = _cvl.autofix_cvl
CVLDiagnostic = _cvl.CVLDiagnostic


def _fence(info: str, body: str) -> str:
    return f"```{info}\n{body}\n```"


# ---------------------------------------------------------------------------
# Extraction selection order (R18.2, R18.3)
# ---------------------------------------------------------------------------


def test_extract_prefers_cvl_fenced_block():
    doc = "rule onlyOwner { assert true; }"
    response = "Here is your spec:\n" + _fence("cvl", doc) + "\ntrailing prose"
    assert extract_cvl(response) == doc


def test_extract_first_cvl_block_when_multiple():
    first = "rule a { assert true; }"
    second = "rule b { assert false; }"
    response = _fence("cvl", first) + "\n" + _fence("cvl", second)
    assert extract_cvl(response) == first


def test_extract_generic_fence_with_keyword():
    doc = "invariant totalIsPositive() total() > 0;"
    response = "prose\n" + _fence("", doc) + "\nmore prose"
    assert extract_cvl(response) == doc


def test_generic_fence_without_keyword_falls_through_to_whole_text():
    # A generic fence holding no CVL keyword must not be selected; the whole
    # response is returned instead.
    response = "using X; rule r { assert true; }\n" + _fence("", "just some numbers 1 2 3")
    out = extract_cvl(response)
    assert "just some numbers" in out
    assert out == response.strip()


def test_whole_text_fallback_when_no_fence():
    response = "  rule r { assert e.msg.sender == owner(); }  "
    assert extract_cvl(response) == "rule r { assert e.msg.sender == owner(); }"


def test_cvl_fence_wins_over_earlier_generic_keyword_fence():
    generic = "using Foo as foo;"
    cvl = "rule r { assert true; }"
    response = _fence("solidity", generic) + "\n" + _fence("cvl", cvl)
    assert extract_cvl(response) == cvl


# ---------------------------------------------------------------------------
# methods_block prepend (R18.6, R18.13)
# ---------------------------------------------------------------------------


def test_methods_block_prepended():
    mb = "methods {\n    function owner() external returns (address) envfree;\n}"
    doc = "rule r { assert true; }"
    out = extract_cvl(_fence("cvl", doc), methods_block=mb)
    assert out == mb + "\n\n" + doc


def test_no_methods_block_returns_body_only():
    doc = "rule r { assert true; }"
    assert extract_cvl(_fence("cvl", doc)) == doc


# ---------------------------------------------------------------------------
# Idempotence (R18.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        _fence("cvl", "rule r { assert true; }"),
        _fence("", "invariant inv() x() > 0;"),
        "using Y as y;\nrule r { assert true; }",
        "  ghost mapping(address => uint256) g;  ",
    ],
)
def test_extract_is_idempotent(response):
    once = extract_cvl(response)
    twice = extract_cvl(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Anti-regression: NONE of the destructive rewrites are applied (R18.5)
# ---------------------------------------------------------------------------


def test_preserves_assume_le():
    doc = "rule r { assume x <= y; assert true; }"
    out = extract_cvl(_fence("cvl", doc))
    assert "assume x <= y;" in out
    assert "< y + 1" not in out


def test_preserves_not_equal_zero():
    doc = "rule r { assume amount != 0; assert true; }"
    out = extract_cvl(_fence("cvl", doc))
    assert "!= 0" in out
    assert "> 0" not in out


def test_preserves_not_equal_address_zero():
    doc = "rule r { assume a != address(0); assert true; }"
    out = extract_cvl(_fence("cvl", doc))
    assert "!= address(0)" in out
    assert "> address(0)" not in out


def test_preserves_invariant_declaration():
    doc = "invariant sumOfBalances() totalSupply() == sumBalances;"
    out = extract_cvl(_fence("cvl", doc))
    assert "invariant sumOfBalances()" in out
    assert "REMOVED" not in out


def test_preserves_tuple_comparison_invariant():
    doc = "invariant pair() (a(), b()) == (c(), d());"
    out = extract_cvl(_fence("cvl", doc))
    assert out == doc


def test_preserves_using_declaration():
    doc = "using Pool as pool;\nrule r { assert true; }"
    out = extract_cvl(doc)
    assert "using Pool as pool;" in out


def test_preserves_not_has_role_negation():
    doc = "rule r { assume !hasRole(ADMIN, e.msg.sender); assert true; }"
    out = extract_cvl(_fence("cvl", doc))
    assert "!hasRole" in out
    assert "not hasRole" not in out


def test_preserves_require_and_comparison_operators():
    doc = "rule r { require a >= b; require c <= d; assert e != f; }"
    out = extract_cvl(_fence("cvl", doc))
    assert "require a >= b;" in out
    assert "require c <= d;" in out
    assert "assert e != f;" in out


# ---------------------------------------------------------------------------
# Validation diagnostics (R18.8)
# ---------------------------------------------------------------------------


def test_diagnostic_duplicated_methods_block():
    # TWO methods blocks: the first is kept, the second is the flagged
    # duplicate. The diagnostic now fires only when 2+ blocks are present.
    mb = "methods {\n    function owner() external returns (address) envfree;\n}"
    text = mb + "\n\n" + mb + "\n\nrule r { assert true; }"
    diags = validate_cvl(text, generated_methods_block=mb)
    cats = {d.category for d in diags}
    assert "duplicated_methods_block" in cats
    dups = [d for d in diags if d.category == "duplicated_methods_block"]
    # Exactly one duplicate is flagged (the second block); the first is kept.
    assert len(dups) == 1
    dup = dups[0]
    # The flagged block is the SECOND one, so it does not start at line 1.
    # First block spans lines 1-3, blank line 4, second block begins at line 5.
    assert dup.line > 1
    assert dup.line == 5


def test_no_duplicated_diagnostic_for_single_methods_block():
    # Regression guard: a SINGLE methods block matching the generated one is the
    # normal, correct case and must NOT be flagged as a duplicate.
    mb = "methods {\n    function owner() external returns (address) envfree;\n}"
    text = mb + "\n\nrule r { assert true; }"
    diags = validate_cvl(text, generated_methods_block=mb)
    assert "duplicated_methods_block" not in {d.category for d in diags}


def test_diagnostic_hook_assignment():
    text = "hook Sstore total uint256 v {\n    ghostTotal := v;\n}"
    diags = validate_cvl(text)
    hooks = [d for d in diags if d.category == "hook_assignment"]
    assert len(hooks) == 1
    assert hooks[0].line == 2


def test_diagnostic_pragma_line():
    text = "pragma solidity ^0.8.0;\nrule r { assert true; }"
    diags = validate_cvl(text)
    prag = [d for d in diags if d.category == "solidity_pragma"]
    assert len(prag) == 1
    assert prag[0].line == 1


def test_diagnostic_license_line():
    text = "// SPDX-License-Identifier: MIT\nrule r { assert true; }"
    diags = validate_cvl(text)
    lic = [d for d in diags if d.category == "license_identifier"]
    assert len(lic) == 1
    assert lic[0].line == 1


def test_diagnostic_no_cvl_document():
    text = "just some prose with numbers 1 2 3 and no declarations"
    diags = validate_cvl(text)
    nocvl = [d for d in diags if d.category == "no_cvl"]
    assert len(nocvl) == 1
    assert nocvl[0].line == 1


def test_no_diagnostic_for_assume_require_comparison_using_invariant():
    text = (
        "using Pool as pool;\n"
        "invariant inv() total() > 0;\n"
        "rule r {\n"
        "    assume x <= y;\n"
        "    require a != 0;\n"
        "    assert b != address(0);\n"
        "}\n"
    )
    diags = validate_cvl(text)
    assert diags == []


def test_validator_does_not_mutate_text():
    text = "pragma solidity ^0.8.0;\nrule r { assume x <= y; assert true; }\n"
    before = text
    validate_cvl(text)
    assert text == before


# ---------------------------------------------------------------------------
# Autofix allowlist (R18.10, R18.11)
# ---------------------------------------------------------------------------


def test_autofix_removes_pragma_and_license_only():
    text = (
        "pragma solidity ^0.8.0;\n"
        "// SPDX-License-Identifier: MIT\n"
        "rule r { assume x <= y; assert b != 0; }\n"
    )
    fixed, applied = autofix_cvl(text)
    rules = {r for _, r in applied}
    assert rules == {"remove_pragma", "remove_license"}
    assert "pragma" not in fixed
    assert "SPDX-License-Identifier" not in fixed
    # The rule line is byte-identical (destructive rewrites are NOT applied).
    assert "rule r { assume x <= y; assert b != 0; }" in fixed


def test_autofix_replaces_hook_assignment():
    text = "hook Sstore total uint256 v {\n    ghostTotal := v;\n}\n"
    fixed, applied = autofix_cvl(text)
    assert ("fix_hook_assignment" in {r for _, r in applied})
    assert ":=" not in fixed
    assert "ghostTotal = v;" in fixed


def test_autofix_removes_duplicated_methods_block():
    # TWO methods blocks: the first is kept, the duplicate (2nd) is removed.
    mb = "methods {\n    function owner() external returns (address) envfree;\n}"
    text = mb + "\n\n" + mb + "\n\nrule r { assert true; }\n"
    fixed, applied = autofix_cvl(text, generated_methods_block=mb)
    assert "remove_methods_block" in {r for _, r in applied}
    # Exactly one methods block survives (the first is kept).
    assert fixed.count("methods {") == 1
    assert len(re.findall(r"\bmethods\s*\{", fixed)) == 1
    assert "rule r { assert true; }" in fixed


def test_autofix_keeps_single_methods_block():
    # A lone methods block is diagnostic-free and must never be removed.
    mb = "methods {\n    function owner() external returns (address) envfree;\n}"
    text = mb + "\n\nrule r { assert true; }\n"
    fixed, applied = autofix_cvl(text, generated_methods_block=mb)
    assert "remove_methods_block" not in {r for _, r in applied}
    # The single block is left intact.
    assert len(re.findall(r"\bmethods\s*\{", fixed)) == 1


def test_autofix_leaves_other_lines_byte_identical():
    text = (
        "using Pool as pool;\n"
        "rule r {\n"
        "    assume x <= y;\n"
        "    require a != 0;\n"
        "    assert b != address(0);\n"
        "}\n"
    )
    fixed, applied = autofix_cvl(text)
    # Nothing in the allowlist matches -> no changes at all.
    assert applied == []
    assert fixed == text


def test_autofix_does_not_touch_assume_or_comparisons():
    text = "pragma solidity ^0.8.0;\nrule r { assume x <= y; assert a != address(0); }\n"
    fixed, _ = autofix_cvl(text)
    assert "assume x <= y;" in fixed
    assert "a != address(0)" in fixed
    assert "< y + 1" not in fixed
    assert "> address(0)" not in fixed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
