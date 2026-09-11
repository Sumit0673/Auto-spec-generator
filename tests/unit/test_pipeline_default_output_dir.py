"""Unit test: the default pipeline output directory is ``spec_pipeline/Output``.

The default output directory is resolved relative to the ``spec_pipeline``
package location (``Path(__file__).parent / "Output"``) so it is stable
regardless of the process cwd, replacing the old ``Path.cwd() /
"spec_pipeline_output"`` default.

``spec_pipeline/__init__.py`` eagerly imports slither-backed stages, so
``pipeline.py`` is loaded by file path with slither-free stubs registered first
(mirroring ``test_pipeline_zero_contract.py``).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_stub_packages() -> None:
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
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_install_stub_packages()

# Ensure a real stage1_extract is loadable (pipeline imports from it).
if "spec_pipeline.stage1_extract" not in sys.modules:
    _load_module_by_path(
        "spec_pipeline.stage1_extract", "spec_pipeline/stage1_extract.py"
    )

if "spec_pipeline._pipeline_default_out_under_test" not in sys.modules:
    pipeline = _load_module_by_path(
        "spec_pipeline._pipeline_default_out_under_test", "spec_pipeline/pipeline.py"
    )
else:  # pragma: no cover
    pipeline = sys.modules["spec_pipeline._pipeline_default_out_under_test"]


def test_default_output_dir_is_spec_pipeline_output():
    expected = _REPO_ROOT / "spec_pipeline" / "Output"
    assert pipeline._default_output_dir() == expected


def test_default_output_dir_is_cwd_independent(tmp_path, monkeypatch):
    # The default must not depend on the process cwd (the old defect).
    monkeypatch.chdir(tmp_path)
    assert pipeline._default_output_dir() == _REPO_ROOT / "spec_pipeline" / "Output"


def test_gitignore_lists_output_dir():
    gitignore = (_REPO_ROOT / ".gitignore").read_text()
    assert "spec_pipeline/Output/" in gitignore


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
