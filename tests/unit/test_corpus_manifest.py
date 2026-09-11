"""Unit tests for the Fixture_Corpus manifest (Task 8.4).

Requirements 19.1, 19.2, 19.9. These tests assert that
``tests/fixtures/corpus/corpus_manifest.json`` describes one project per
Contract_Shape_Taxonomy shape, that each named project directory exists under
``tests/fixtures/corpus/``, that every declared ``expected_outcome`` is a valid
member of :class:`spec_pipeline.outcomes.Outcome`, and that each project
directory carries the marker files / sources its shape requires.

``spec_pipeline.outcomes`` is pure (stdlib only), so it imports directly even
where slither is uninstallable; a defensive importlib fallback mirrors the other
unit tests in case the package ``__init__`` ever changes.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

try:  # Pure module: normally importable directly.
    from spec_pipeline.outcomes import Outcome  # type: ignore
except Exception:  # pragma: no cover - defensive: load the pure module directly
    _OUTCOMES_PATH = (
        Path(__file__).resolve().parents[2] / "spec_pipeline" / "outcomes.py"
    )
    _MODNAME = "spec_pipeline_outcomes_for_corpus_test"
    _spec = importlib.util.spec_from_file_location(_MODNAME, _OUTCOMES_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_MODNAME] = _mod
    _spec.loader.exec_module(_mod)
    Outcome = _mod.Outcome


# The 12 Contract_Shape_Taxonomy shapes (Requirement 19.1), each mapped to the
# project directory that realizes it.
_EXPECTED_SHAPES: dict[str, str] = {
    "single_file": "single_file",
    "multi_contract": "multi_contract",
    "inheritance_depth": "inheritance_depth",
    "library_dependent": "library_dependent",
    "struct_enum_signature": "struct_enum_signature",
    "upgradeable_proxy": "upgradeable_proxy",
    "foundry_layout": "foundry_layout",
    "hardhat_layout": "hardhat_layout",
    "npm_dependency": "npm_dependency",
    "mixed_pragmas": "mixed_pragmas",
    "interface_only": "interface_only",
    "abstract_contract": "abstract_contract",
}

_CORPUS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_MANIFEST_PATH = _CORPUS_ROOT / "corpus_manifest.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Load and return the parsed corpus manifest."""
    assert _MANIFEST_PATH.is_file(), f"missing manifest: {_MANIFEST_PATH}"
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def shapes(manifest: dict) -> dict:
    """Return the manifest ``shapes`` mapping."""
    assert "shapes" in manifest, "manifest lacks a top-level 'shapes' mapping"
    return manifest["shapes"]


def test_all_twelve_shapes_present(shapes: dict) -> None:
    """All 12 Contract_Shape_Taxonomy shapes have an entry (R19.1)."""
    assert set(shapes) == set(_EXPECTED_SHAPES), (
        "manifest shapes do not match the taxonomy; "
        f"missing={set(_EXPECTED_SHAPES) - set(shapes)} "
        f"extra={set(shapes) - set(_EXPECTED_SHAPES)}"
    )
    assert len(shapes) == 12


@pytest.mark.parametrize("shape", sorted(_EXPECTED_SHAPES))
def test_project_dir_exists(shapes: dict, shape: str) -> None:
    """Each declared project_dir exists under tests/fixtures/corpus/ (R19.1)."""
    entry = shapes[shape]
    assert "project_dir" in entry, f"{shape}: no project_dir"
    project_dir = _CORPUS_ROOT / entry["project_dir"]
    assert project_dir.is_dir(), f"{shape}: missing project dir {project_dir}"


@pytest.mark.parametrize("shape", sorted(_EXPECTED_SHAPES))
def test_expected_outcome_is_valid(shapes: dict, shape: str) -> None:
    """Each expected_outcome is a valid Outcome member (R19.2, R19.9)."""
    entry = shapes[shape]
    assert "expected_outcome" in entry, f"{shape}: no expected_outcome"
    # Constructing the Outcome raises ValueError for a non-member.
    outcome = Outcome(entry["expected_outcome"])
    assert outcome.value == entry["expected_outcome"]


def test_notes_present(shapes: dict) -> None:
    """Each shape documents the tool dependency in a notes field (R19.9)."""
    for shape, entry in shapes.items():
        assert entry.get("notes"), f"{shape}: empty or missing notes"


def _sol_files(project_dir: Path) -> list[Path]:
    return sorted(project_dir.rglob("*.sol"))


def test_foundry_layout_marker() -> None:
    """foundry_layout carries a foundry.toml (R19.1)."""
    assert (_CORPUS_ROOT / "foundry_layout" / "foundry.toml").is_file()


def test_hardhat_layout_marker() -> None:
    """hardhat_layout carries a hardhat.config.js (R19.1)."""
    assert (_CORPUS_ROOT / "hardhat_layout" / "hardhat.config.js").is_file()


def test_npm_dependency_marker() -> None:
    """npm_dependency has a .sol under node_modules/*/* (R19.1)."""
    matches = list(
        (_CORPUS_ROOT / "npm_dependency" / "node_modules").rglob("*.sol")
    )
    assert matches, "npm_dependency: no node_modules/**/*.sol dependency source"


def test_interface_only_declares_only_interface() -> None:
    """interface_only's .sol declares only `interface` types (R19.1)."""
    sol_files = _sol_files(_CORPUS_ROOT / "interface_only")
    assert sol_files, "interface_only: no .sol files"
    for sol in sol_files:
        text = sol.read_text(encoding="utf-8")
        assert re.search(r"\binterface\s+\w+", text), (
            f"interface_only: {sol.name} declares no interface"
        )
        # No concrete/abstract contract declarations.
        assert not re.search(r"(?<!abstract\s)(?<!\w)contract\s+\w+", text), (
            f"interface_only: {sol.name} declares a contract"
        )


def test_abstract_contract_declares_abstract_contract() -> None:
    """abstract_contract declares an `abstract contract` (R19.1)."""
    sol_files = _sol_files(_CORPUS_ROOT / "abstract_contract")
    assert sol_files, "abstract_contract: no .sol files"
    joined = "\n".join(s.read_text(encoding="utf-8") for s in sol_files)
    assert re.search(r"\babstract\s+contract\s+\w+", joined), (
        "abstract_contract: no `abstract contract` declaration"
    )


def test_mixed_pragmas_have_differing_pragmas() -> None:
    """mixed_pragmas has >=2 .sol files with differing pragma solidity (R19.1)."""
    sol_files = _sol_files(_CORPUS_ROOT / "mixed_pragmas")
    assert len(sol_files) >= 2, "mixed_pragmas: fewer than two .sol files"
    pragmas: set[str] = set()
    for sol in sol_files:
        text = sol.read_text(encoding="utf-8")
        m = re.search(r"pragma\s+solidity\s+([^;]+);", text)
        assert m, f"mixed_pragmas: {sol.name} has no pragma solidity"
        pragmas.add(m.group(1).strip())
    assert len(pragmas) >= 2, (
        f"mixed_pragmas: expected differing pragmas, found {pragmas}"
    )
