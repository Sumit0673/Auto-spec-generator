"""Prompt_Lint (Requirement 15.5, 15.8).

Scans the prompt template LITERALS in ``spec_pipeline/prompts.py`` for any
project-specific identifier from the denylist. If any denylisted identifier is
present (case-insensitive substring match) in any string constant, the check
fails, naming the offending template constant and the identifier. Otherwise it
passes.

Usage:
    python -m spec_pipeline.prompt_lint          # lint the real prompts module
    from spec_pipeline.prompt_lint import lint_prompts, main

The module loads ``prompts.py`` BY FILE PATH via importlib so it never triggers
the slither-backed ``spec_pipeline`` package ``__init__``; the check therefore
runs in any environment (R15 note: prompts.py stays import-safe without slither).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# The project-specific denylist fixed by Requirement 15.5.
DENYLIST: tuple[str, ...] = (
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

_PROMPTS_PATH = Path(__file__).resolve().parent / "prompts.py"


def _load_prompts_module(path: Path | None = None) -> ModuleType:
    """Load prompts.py by file path (avoids the slither-heavy package __init__)."""
    path = Path(path) if path is not None else _PROMPTS_PATH
    mod_name = "spec_pipeline_prompts_under_lint"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load prompts module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _iter_string_constants(mod: ModuleType):
    """Yield (name, value) for every module-level ``str`` constant."""
    for name in dir(mod):
        if name.startswith("__"):
            continue
        value = getattr(mod, name)
        if isinstance(value, str):
            yield name, value


def lint_prompts(mod: ModuleType | None = None) -> list[tuple[str, str]]:
    """Return a list of (template_name, identifier) violations.

    Each entry names a prompt template constant whose literal contains a
    denylisted identifier (case-insensitive substring). An empty list means the
    prompts are clean.
    """
    if mod is None:
        mod = _load_prompts_module()

    violations: list[tuple[str, str]] = []
    lowered_denylist = [(ident, ident.lower()) for ident in DENYLIST]
    for name, value in _iter_string_constants(mod):
        haystack = value.lower()
        for ident, ident_lower in lowered_denylist:
            if ident_lower in haystack:
                violations.append((name, ident))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit 1 (and report) on any violation, else exit 0."""
    argv = list(sys.argv[1:] if argv is None else argv)
    path = Path(argv[0]) if argv else None

    mod = _load_prompts_module(path)
    violations = lint_prompts(mod)

    if violations:
        print("Prompt_Lint: FAIL — project-specific identifiers found in prompt templates:")
        for template_name, identifier in violations:
            print(f"  template {template_name!r} contains denylisted identifier {identifier!r}")
        return 1

    print(f"Prompt_Lint: OK — no denylisted identifiers in {len(DENYLIST)}-item denylist")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
