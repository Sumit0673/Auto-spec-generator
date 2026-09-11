"""Unit tests for Prompt_Lint and the project-agnostic Prompt_Builder (R15).

Both modules under test (``spec_pipeline.prompts`` and
``spec_pipeline.prompt_lint``) are slither-free, but the ``spec_pipeline``
package ``__init__`` eagerly imports slither. We therefore load the modules BY
FILE PATH via importlib (mirroring tests/unit/test_methods_block.py), so the
suite runs with slither absent.

Covers:
- R15.5/R15.8: Prompt_Lint exits 1 (and names the template + identifier) on a
  planted denylisted identifier, and 0 on the real cleaned templates.
- R15.6: the CVL syntax guidance is character-identical across the Stage 3
  system prompt, the Stage 3 feedback system prompt, and the Stage 4 repair
  system prompt.
- R15.1/R15.2/R15.3/R15.4/R15.7/R15.9: the assembled per-contract sections use
  ONLY identifiers present in the provided types / Stage 1 entry (metamorphic-ish
  alpha-rename check), and the type-exclusion section appears only when a
  struct/enum is detected.
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
def prompts() -> ModuleType:
    try:
        import spec_pipeline.prompts as p  # type: ignore

        return p
    except Exception:  # pragma: no cover - slither absent
        return _load_by_path("prompts_under_test", "spec_pipeline/prompts.py")


@pytest.fixture(scope="module")
def prompt_lint() -> ModuleType:
    try:
        import spec_pipeline.prompt_lint as pl  # type: ignore

        return pl
    except Exception:  # pragma: no cover - slither absent
        return _load_by_path("prompt_lint_under_test", "spec_pipeline/prompt_lint.py")


# ---------------------------------------------------------------------------
# Duck-typed Stage 1 stand-ins (match how the builder reads the table)
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
class Contract:
    name: str
    function_gates: list = field(default_factory=list)


# ===========================================================================
# R15.5 / R15.8 — Prompt_Lint
# ===========================================================================


def test_lint_passes_on_real_cleaned_prompts(prompt_lint):
    """The real prompts module must be clean (exit 0, no violations)."""
    violations = prompt_lint.lint_prompts()  # loads the real prompts.py
    assert violations == [], f"unexpected violations: {violations}"
    assert prompt_lint.main([]) == 0


def test_lint_denylist_has_all_ten_identifiers(prompt_lint):
    expected = {
        "poolId",
        "PoolSettings",
        "LPConfig",
        "AdminRnR",
        "FirstLossCoverConfig",
        "FrontLoadingFeesStructure",
        "FeeStructure",
        "PoolConfig",
        "onlyPoolOwnerOrHumaOwner",
        "deployPool",
    }
    assert set(prompt_lint.DENYLIST) == expected


def test_lint_fails_on_planted_identifier(prompt_lint):
    """A module carrying a denylisted identifier is flagged with template+id."""
    fake = ModuleType("fake_prompts")
    fake.CLEAN_TEMPLATE = "Write ONE parametric rule using method f."
    fake.STAGE_X_SYSTEM = "Complex struct types like PoolSettings are not CVL types."

    violations = prompt_lint.lint_prompts(fake)
    assert ("STAGE_X_SYSTEM", "PoolSettings") in violations
    # The clean template is not flagged.
    assert not any(name == "CLEAN_TEMPLATE" for name, _ in violations)


def test_lint_is_case_insensitive(prompt_lint):
    fake = ModuleType("fake_prompts_ci")
    fake.T = "call deploypool(e, args) here"  # lower-case variant
    violations = prompt_lint.lint_prompts(fake)
    assert ("T", "deployPool") in violations


def test_lint_main_exit_1_on_planted(prompt_lint, tmp_path):
    """The CLI entry point returns exit code 1 when given a dirty prompts file."""
    dirty = tmp_path / "dirty_prompts.py"
    dirty.write_text(
        'STAGE3_SYSTEM = "Do not write 22 rules behind onlyPoolOwnerOrHumaOwner."\n'
    )
    assert prompt_lint.main([str(dirty)]) == 1


def test_lint_main_exit_0_on_clean_file(prompt_lint, tmp_path):
    clean = tmp_path / "clean_prompts.py"
    clean.write_text('STAGE3_SYSTEM = "Write ONE parametric rule using method f."\n')
    assert prompt_lint.main([str(clean)]) == 0


# ===========================================================================
# R15.6 — one shared CVL guidance, character-identical across three prompts
# ===========================================================================


def test_cvl_guidance_present_and_identical_across_three_system_prompts(prompts):
    guidance = prompts.CVL_SYNTAX_GUIDANCE
    assert guidance  # non-empty
    for name in ("STAGE3_SYSTEM", "STAGE3_FEEDBACK_SYSTEM", "STAGE4_REPAIR_SYSTEM"):
        sys_prompt = getattr(prompts, name)
        assert guidance in sys_prompt, f"{name} missing shared guidance"

    # The embedded substrings must be character-identical to each other.
    def _slice(sys_prompt: str) -> str:
        i = sys_prompt.index(guidance)
        return sys_prompt[i : i + len(guidance)]

    s3 = _slice(prompts.STAGE3_SYSTEM)
    s3f = _slice(prompts.STAGE3_FEEDBACK_SYSTEM)
    s4r = _slice(prompts.STAGE4_REPAIR_SYSTEM)
    assert s3 == s3f == s4r == guidance


def test_user_template_renders_guidance_with_single_braces(prompts):
    out = prompts.format_stage3_user("TBL", {"C": {}}, "src", "METHODS")
    # A brace-bearing guidance line must render literally (not escaped/doubled).
    assert "hook Sstore counter uint256 newVal { ghostCounter = newVal; }" in out
    assert "{{" not in out and "}}" not in out


# ===========================================================================
# R15.4 / R15.1 — type guidance appears only when a struct/enum is detected
# ===========================================================================


def test_type_guidance_omitted_when_no_types(prompts):
    assert prompts.build_type_guidance([]) == ""


def test_type_guidance_names_detected_types(prompts):
    out = prompts.build_type_guidance(["Widget", "Mode"])
    assert "Widget" in out and "Mode" in out
    assert "TYPE GUIDANCE" in out


# ===========================================================================
# R15.9 — syntax example omitted when no external/public non-special function
# ===========================================================================


def test_syntax_example_omitted_without_functions(prompts):
    c = Contract("Empty", function_gates=[])
    assert prompts.build_syntax_example(c) == ""


def test_syntax_example_omitted_for_only_constructor(prompts):
    c = Contract("Only", function_gates=[FG("constructor", "()", is_constructor=True)])
    assert prompts.build_syntax_example(c) == ""


def test_syntax_example_built_from_a_function_signature(prompts):
    c = Contract(
        "Widget",
        function_gates=[FG("spin", "(uint256 rounds) -> bool", mutability="nonpayable")],
    )
    out = prompts.build_syntax_example(c)
    assert "spin(e, rounds)" in out
    assert "uint256 rounds;" in out


# ===========================================================================
# R15.7 — metamorphic: assembled sections use ONLY provided identifiers
# ===========================================================================


def _assemble_sections(prompts, types, contract) -> str:
    return (
        prompts.build_type_guidance(types)
        + prompts.build_cohort_guidance(contract)
        + prompts.build_syntax_example(contract)
    )


def test_cohort_guidance_uses_only_provided_identifiers(prompts):
    c = Contract(
        "Vault",
        function_gates=[
            FG("deposit", "(uint256 amt)", modifier="onlyOwner"),
            FG("withdraw", "(uint256 amt)", modifier="onlyOwner"),
            FG("peek", "() -> uint256", modifier="none", mutability="view"),
        ],
    )
    out = prompts.build_cohort_guidance(c)
    for ident in ("Vault", "deposit", "withdraw", "peek", "onlyOwner"):
        assert ident in out


def test_alpha_rename_changes_sections_only_at_identifier_positions(prompts):
    # Injective rename of every identifier the sections may reference.
    mapping = {
        "Vault": "Zzz1",
        "deposit": "aaa1",
        "withdraw": "aaa2",
        "onlyOwner": "mmm1",
        "amt": "ppp1",
        "uint256": "uint256",  # types are not renamed (kept identical)
        "bool": "bool",
    }

    def mk(name_of, dep, wd, mod, param):
        return Contract(
            name_of,
            function_gates=[
                FG(dep, f"(uint256 {param})", modifier=mod),
                FG(wd, f"(uint256 {param})", modifier=mod),
            ],
        )

    original = mk("Vault", "deposit", "withdraw", "onlyOwner", "amt")
    renamed = mk("Zzz1", "aaa1", "aaa2", "mmm1", "ppp1")

    out_orig = _assemble_sections(prompts, [], original)
    out_renamed = _assemble_sections(prompts, [], renamed)

    # Applying the same injective mapping to the original output must reproduce
    # the renamed output exactly: identifiers change, everything else is fixed.
    transformed = out_orig
    # Replace longest identifiers first to avoid substring collisions.
    for src in sorted(mapping, key=len, reverse=True):
        transformed = transformed.replace(src, mapping[src])
    assert transformed == out_renamed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
