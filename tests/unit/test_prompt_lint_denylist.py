"""Unit test: Prompt_Lint denylist (Task 12.3, validates Requirement 15.5).

Asserts that Prompt_Lint exits 1 and names the offending template for EACH
denylisted Huma identifier, and exits 0 on clean templates.

The existing tests/unit/test_prompts_lint.py exercises Prompt_Lint against a
couple of planted identifiers; this file completes the coverage by asserting the
per-identifier behavior for all ten denylist members individually (R15.5).

``spec_pipeline.prompt_lint`` is slither-free, but the ``spec_pipeline`` package
``__init__`` eagerly imports slither. We therefore load the module BY FILE PATH
via importlib (mirroring tests/unit/test_prompts_lint.py) so the suite runs with
slither absent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The ten project-specific Huma identifiers fixed by Requirement 15.5.
_EXPECTED_DENYLIST = (
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
)


def _load_by_path(mod_name: str, rel_path: str) -> ModuleType:
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def prompt_lint() -> ModuleType:
    try:
        import spec_pipeline.prompt_lint as pl  # type: ignore

        return pl
    except Exception:  # pragma: no cover - slither absent
        return _load_by_path("prompt_lint_denylist_under_test", "spec_pipeline/prompt_lint.py")


# ===========================================================================
# Per-identifier: exit 1 naming the offending template
# ===========================================================================


@pytest.mark.parametrize("identifier", _EXPECTED_DENYLIST)
def test_lint_flags_each_denylisted_identifier(prompt_lint, identifier):
    """Each denylisted identifier is detected and the offending template named."""
    template_name = "STAGE_UNDER_TEST_SYSTEM"
    fake = ModuleType("fake_prompts_per_identifier")
    # A clean sibling template must NOT be flagged.
    fake.CLEAN_SIBLING = "Write ONE parametric rule using method f."
    setattr(fake, template_name, f"Encode the {identifier} invariant directly.")

    violations = prompt_lint.lint_prompts(fake)

    # The offending template is named together with the exact identifier.
    assert (template_name, identifier) in violations
    # The clean sibling template is never flagged.
    assert not any(name == "CLEAN_SIBLING" for name, _ in violations)


@pytest.mark.parametrize("identifier", _EXPECTED_DENYLIST)
def test_lint_main_exit_1_naming_template_per_identifier(prompt_lint, tmp_path, identifier, capsys):
    """The CLI exits 1 for each denylisted identifier and prints the template name."""
    template_name = "STAGE3_SYSTEM"
    dirty = tmp_path / f"dirty_{identifier}.py"
    dirty.write_text(f'{template_name} = "Reference to {identifier} lives here."\n')

    exit_code = prompt_lint.main([str(dirty)])
    assert exit_code == 1

    out = capsys.readouterr().out
    assert template_name in out
    assert identifier in out


# ===========================================================================
# Clean templates: exit 0
# ===========================================================================


def test_lint_exit_0_on_clean_module(prompt_lint):
    """A module free of denylisted identifiers yields no violations."""
    fake = ModuleType("fake_prompts_clean")
    fake.STAGE3_SYSTEM = "Write ONE parametric rule using method f for the counter."
    fake.STAGE3_USER_TEMPLATE = "Encode the given invariants; do not rediscover them."
    fake.STAGE4_REPAIR_SYSTEM = "Apply the critic findings and keep correct rules."

    assert prompt_lint.lint_prompts(fake) == []


def test_lint_main_exit_0_on_clean_file(prompt_lint, tmp_path):
    """The CLI exits 0 on a clean prompts file."""
    clean = tmp_path / "clean_prompts.py"
    clean.write_text(
        'STAGE3_SYSTEM = "Write ONE parametric rule using method f."\n'
        'STAGE3_USER_TEMPLATE = "Encode the given invariants directly."\n'
    )
    assert prompt_lint.main([str(clean)]) == 0


def test_lint_exit_0_on_real_prompts_module(prompt_lint):
    """The real (cleaned) prompts.py must remain clean."""
    assert prompt_lint.lint_prompts() == []
    assert prompt_lint.main([]) == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
