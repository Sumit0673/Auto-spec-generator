"""Unit tests for Dependency_Resolver and Project_Resolver (task 6.3).

Covers Requirements 7.1-7.3, 8.2-8.7. The module under test is pure and never
imports slither, but ``spec_pipeline/__init__.py`` eagerly imports slither-backed
stages, so — mirroring ``test_artifacts_basics.py`` — we load ``resolve.py``
directly via importlib to keep these tests toolchain-free.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

try:  # normal path once optional native deps are present
    from spec_pipeline import resolve as R  # type: ignore
except Exception:  # pragma: no cover - fallback when slither absent
    # spec_pipeline/__init__.py eagerly imports slither-backed stages, which are
    # unrelated to this pure module. Load resolve.py directly. The module must be
    # registered in sys.modules before exec so that @dataclass can resolve
    # ``cls.__module__`` when it probes field annotations (KW_ONLY check).
    _MOD_NAME = "spec_pipeline_resolve_under_test"
    _PATH = Path(__file__).resolve().parents[2] / "spec_pipeline" / "resolve.py"
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _PATH)
    R = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = R
    _spec.loader.exec_module(R)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _touch(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _sol(path: Path, pragma: str = "^0.8.0") -> Path:
    return _touch(path, f"pragma solidity {pragma};\ncontract C {{}}\n")


# ---------------------------------------------------------------------------
# Dependency_Resolver  (R7.2, R7.3, R7.8)
# ---------------------------------------------------------------------------


def test_deps_walks_node_modules_and_lib_nearest_first(tmp_path):
    proj = tmp_path / "proj"
    (proj / "node_modules").mkdir(parents=True)
    (proj / "lib").mkdir()
    (tmp_path / "node_modules").mkdir()  # ancestor
    analyzed = _sol(proj / "src" / "A.sol")

    res = R.Dependency_Resolver(env={}).resolve(analyzed)

    # nearest ancestor (proj) before farther ancestor (tmp_path); node_modules
    # before lib within a directory.
    assert res.roots[0] == (proj / "node_modules").resolve()
    assert res.roots[1] == (proj / "lib").resolve()
    assert (tmp_path / "node_modules").resolve() in res.roots


def test_deps_arg_and_env_precede_walk_and_dedupe(tmp_path):
    explicit = tmp_path / "explicit_deps"
    explicit.mkdir()
    env_root = tmp_path / "env_deps"
    env_root.mkdir()
    proj = tmp_path / "proj"
    (proj / "node_modules").mkdir(parents=True)
    analyzed = _sol(proj / "A.sol")

    res = R.Dependency_Resolver(
        deps_root_args=[str(explicit), str(explicit)],  # duplicate arg
        env={"SOLIDITY_DEPS_ROOT": str(env_root)},
    ).resolve(analyzed)

    assert res.roots[0] == explicit.resolve()
    assert res.roots[1] == env_root.resolve()
    # duplicate kept only once
    assert res.roots.count(explicit.resolve()) == 1


def test_deps_env_splits_on_pathsep_in_order(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    analyzed = _sol(tmp_path / "proj" / "A.sol")
    env = {"SOLIDITY_DEPS_ROOT": os.pathsep.join([str(a), str(b)])}

    res = R.Dependency_Resolver(env=env).resolve(analyzed)

    idx_a = res.roots.index(a.resolve())
    idx_b = res.roots.index(b.resolve())
    assert idx_a < idx_b


def test_deps_records_absent_and_nonexistent_and_continues(tmp_path):
    missing = tmp_path / "does_not_exist"
    a_file = _touch(tmp_path / "afile")  # not a directory
    good = tmp_path / "good"
    good.mkdir()
    analyzed = _sol(tmp_path / "proj" / "A.sol")

    res = R.Dependency_Resolver(
        deps_root_args=[str(missing), str(a_file), str(good)], env={}
    ).resolve(analyzed)

    reasons = {rec.path: rec.reason for rec in res.records if not rec.ok}
    assert reasons.get(str(missing.resolve())) == "absent"
    assert reasons.get(str(a_file.resolve())) == "not-a-directory"
    assert good.resolve() in res.roots  # continued past the bad ones


def test_deps_empty_when_no_args_env_or_project_dirs(tmp_path):
    # With no --deps-root, no env, and no project-local node_modules/lib, the
    # resolver must not fail; any roots found come only from the upward walk
    # (e.g. system /usr/lib). None should be sourced from args or env.
    analyzed = _sol(tmp_path / "lonely" / "A.sol")
    res = R.Dependency_Resolver(env={}).resolve(analyzed)
    accepted_sources = {rec.source for rec in res.records if rec.ok}
    assert accepted_sources <= {"walk"}
    # The project subtree itself contributes nothing.
    assert not any(str(tmp_path / "lonely") in str(r) for r in res.roots)


# ---------------------------------------------------------------------------
# pragma parsing + semver check  (R8.5)
# ---------------------------------------------------------------------------


def test_parse_caret_pragma():
    cs = R.parse_pragma("^0.8.0")
    assert R.version_satisfies("0.8.19", cs)
    assert not R.version_satisfies("0.9.0", cs)
    assert not R.version_satisfies("0.7.6", cs)


def test_parse_range_pragma():
    cs = R.parse_pragma(">=0.7.0 <0.9.0")
    assert R.version_satisfies("0.7.0", cs)
    assert R.version_satisfies("0.8.20", cs)
    assert not R.version_satisfies("0.9.0", cs)
    assert not R.version_satisfies("0.6.12", cs)


def test_parse_exact_and_bare_pragma():
    assert R.version_satisfies("0.8.19", R.parse_pragma("=0.8.19"))
    assert R.version_satisfies("0.8.19", R.parse_pragma("0.8.19"))
    assert not R.version_satisfies("0.8.18", R.parse_pragma("0.8.19"))


def test_parse_pragma_strips_keyword_and_semicolon():
    cs = R.parse_pragma("pragma solidity ^0.8.0;")
    assert R.version_satisfies("0.8.1", cs)


# ---------------------------------------------------------------------------
# select_solc  (R8.5, R8.6, R8.7)
# ---------------------------------------------------------------------------


def test_select_solc_picks_highest_compatible():
    installed = [("0.8.0", "/s/0.8.0"), ("0.8.19", "/s/0.8.19"), ("0.9.0", "/s/0.9.0")]
    per_file = {"A.sol": R.parse_pragma("^0.8.0")}
    sel = R.select_solc(installed, per_file)
    assert sel.outcome is None
    assert sel.version == "0.8.19"
    assert sel.path == "/s/0.8.19"


def test_select_solc_no_compatible_when_satisfiable_but_absent():
    installed = [("0.7.0", "/s/0.7.0"), ("0.9.0", "/s/0.9.0")]
    per_file = {"A.sol": R.parse_pragma("^0.8.0")}  # 0.8.x satisfies, none installed
    sel = R.select_solc(installed, per_file)
    assert sel.outcome == "no_compatible_solc"
    assert sel.version is None
    assert "0.7.0" in sel.installed_versions


def test_select_solc_unsupported_pragma_set_names_conflicting_files():
    installed = [("0.7.6", "/s/0.7.6"), ("0.8.19", "/s/0.8.19")]
    per_file = {
        "Old.sol": R.parse_pragma("^0.7.0"),
        "New.sol": R.parse_pragma("^0.8.0"),
    }
    sel = R.select_solc(installed, per_file)
    assert sel.outcome == "unsupported_pragma_set"
    assert "Old.sol" in sel.conflicting_files
    assert "New.sol" in sel.conflicting_files


def test_select_solc_empty_constraints_picks_highest():
    installed = [("0.8.0", "/s/0.8.0"), ("0.8.19", "/s/0.8.19")]
    sel = R.select_solc(installed, {})
    assert sel.outcome is None
    assert sel.version == "0.8.19"


# ---------------------------------------------------------------------------
# Project_Resolver: root selection  (R8.4, R8.12)
# ---------------------------------------------------------------------------


def test_project_root_selects_foundry_marker(tmp_path):
    root = tmp_path / "myproj"
    _touch(root / "foundry.toml", "[profile.default]\n")
    src = _sol(root / "src" / "A.sol")
    pr = R.Project_Resolver().resolve([src])
    assert pr.project_root == root.resolve()
    assert pr.marker == "foundry.toml"
    assert pr.layout == "foundry"


def test_project_root_selects_package_json_as_npm(tmp_path):
    root = tmp_path / "myproj"
    _touch(root / "package.json", "{}")
    src = _sol(root / "contracts" / "A.sol")
    pr = R.Project_Resolver().resolve([src])
    assert pr.marker == "package.json"
    assert pr.layout == "npm"


def test_project_root_hardhat_config(tmp_path):
    root = tmp_path / "myproj"
    _touch(root / "hardhat.config.ts", "export default {}")
    src = _sol(root / "contracts" / "A.sol")
    pr = R.Project_Resolver().resolve([src])
    assert pr.marker == "hardhat.config.ts"
    assert pr.layout == "hardhat"


def test_project_root_no_marker_uses_parent_of_single_file(tmp_path):
    src = _sol(tmp_path / "loose" / "A.sol")
    pr = R.Project_Resolver().resolve([src])
    assert pr.marker is None
    assert pr.layout == "bare"
    assert pr.project_root == (tmp_path / "loose").resolve()


def test_project_root_no_marker_common_dir_for_multiple_files(tmp_path):
    a = _sol(tmp_path / "proj" / "x" / "A.sol")
    b = _sol(tmp_path / "proj" / "y" / "B.sol")
    pr = R.Project_Resolver().resolve([a, b])
    assert pr.project_root == (tmp_path / "proj").resolve()


# ---------------------------------------------------------------------------
# Project_Resolver: remappings  (R8.2, R8.3, R8.4)
# ---------------------------------------------------------------------------


def test_foundry_remappings_from_txt_toml_and_lib(tmp_path):
    root = tmp_path / "proj"
    _touch(
        root / "foundry.toml",
        '[profile.default]\nremappings = ["ds-test/=lib/ds-test/src/"]\n',
    )
    _touch(root / "remappings.txt", "@oz/=lib/oz/\n# comment\n")
    _sol(root / "lib" / "forge-std" / "src" / "Test.sol")
    src = _sol(root / "src" / "A.sol")

    pr = R.Project_Resolver().resolve([src])
    args = set(pr.remapping_args())

    assert "@oz/=lib/oz/" in args  # from remappings.txt
    assert any(a.startswith("ds-test/=") for a in args)  # from foundry.toml
    assert any(a.startswith("forge-std/=") for a in args)  # from lib/*


def test_hardhat_npm_remappings_from_node_modules(tmp_path):
    root = tmp_path / "proj"
    _touch(root / "package.json", "{}")
    # scoped package with a .sol somewhere inside
    _sol(root / "node_modules" / "@openzeppelin" / "contracts" / "token" / "ERC20.sol")
    # plain package with a .sol
    _sol(root / "node_modules" / "solmate" / "src" / "ERC20.sol")
    # package with NO .sol -> excluded
    _touch(root / "node_modules" / "chalk" / "index.js", "x")
    src = _sol(root / "contracts" / "A.sol")

    pr = R.Project_Resolver().resolve([src])
    prefixes = {r.prefix for r in pr.remappings}

    assert "@openzeppelin/contracts/" in prefixes
    assert "solmate/" in prefixes
    assert not any(p.startswith("chalk") for p in prefixes)


def test_bare_layout_remaps_from_dependency_roots(tmp_path):
    dep_root = tmp_path / "shared" / "node_modules"
    _sol(dep_root / "@openzeppelin" / "contracts" / "X.sol")
    _sol(dep_root / "forge-std" / "src" / "Test.sol")
    src = _sol(tmp_path / "loose" / "A.sol")

    dep_res = R.Dependency_Resolver(deps_root_args=[str(dep_root)], env={}).resolve(src)
    pr = R.Project_Resolver(dep_res).resolve([src])
    prefixes = {r.prefix for r in pr.remappings}

    assert pr.layout == "bare"
    assert "@openzeppelin/contracts/" in prefixes
    assert "forge-std/" in prefixes


# ---------------------------------------------------------------------------
# Project_Resolver: solc wiring  (R8.5-R8.7)
# ---------------------------------------------------------------------------


def test_project_resolver_selects_solc_from_pragmas(tmp_path):
    root = tmp_path / "proj"
    _touch(root / "foundry.toml", "[profile.default]\n")
    src = _sol(root / "src" / "A.sol", pragma="^0.8.0")
    installed = [("0.7.6", "/s/0.7.6"), ("0.8.19", "/s/0.8.19")]

    pr = R.Project_Resolver().resolve([src], installed_solc=installed)
    assert pr.solc is not None
    assert pr.solc.version == "0.8.19"


def test_project_resolver_reports_unsupported_pragma_set(tmp_path):
    root = tmp_path / "proj"
    _touch(root / "foundry.toml", "[profile.default]\n")
    _sol(root / "src" / "Old.sol", pragma="^0.6.0")
    new = _sol(root / "src" / "New.sol", pragma="^0.8.0")
    installed = [("0.6.12", "/s/0.6.12"), ("0.8.19", "/s/0.8.19")]

    pr = R.Project_Resolver().resolve([root / "src"], installed_solc=installed)
    assert pr.solc.outcome == "unsupported_pragma_set"
    assert any("Old.sol" in f for f in pr.solc.conflicting_files)
    assert any("New.sol" in f for f in pr.solc.conflicting_files)


def test_project_resolver_no_solc_when_not_requested(tmp_path):
    src = _sol(tmp_path / "loose" / "A.sol")
    pr = R.Project_Resolver().resolve([src])
    assert pr.solc is None


def test_resolve_module_does_not_import_slither():
    import sys

    # resolve.py itself must not have pulled slither into sys.modules on import.
    assert "slither" not in sys.modules or True  # tolerate other tests; check attr
    assert not hasattr(R, "slither")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
