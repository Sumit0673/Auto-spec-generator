"""Unit tests for the Preflight_Checker (task 6.1, Requirements 6.1-6.7).

These tests never touch the network and never invoke a real prover or compiler.
Tests that assert the binary tools resolve as *absent* force that condition
hermetically (Requirement 10.2) rather than depending on host-installed
tooling: ``slither``/``solc`` are installed in the project's venv (and its
``bin`` directory is searched before ``PATH``), so relying on the host to lack
them is not hermetic. The ``_force_tools_absent`` helper below monkeypatches the
checker's runnable-file discovery (``Preflight_Checker._is_runnable``) to report
nothing as runnable and points the injected env at an empty ``PATH``, so the
interpreter-bin-dir search, the ``PATH`` search, and any absolute-path probing
all find nothing regardless of what is installed on the machine. Interpreter-dir
-before-PATH ordering is proven separately with a temp dir holding a fake
executable placed on ``PATH``; the interpreter directory is checked first, so a
fake on ``PATH`` never wins over an interpreter-dir binary but is found when
nothing else provides the name.

``spec_pipeline/__init__.py`` eagerly imports the slither-backed stages (slither
is uninstallable here), so ``preflight.py`` is loaded directly via importlib,
mirroring the fallback in ``tests/unit/test_artifacts_basics.py``.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

try:  # Normal path once slither is installed.
    from spec_pipeline.preflight import (  # type: ignore
        Preflight_Checker,
        PreflightResult,
        ToolResult,
        required_tools_for_stages,
        stages_requiring_llm,
        UNKNOWN_VERSION,
        REDACTED,
    )
except Exception:  # pragma: no cover - slither absent: load the pure module directly
    _PREFLIGHT_PATH = (
        Path(__file__).resolve().parents[2] / "spec_pipeline" / "preflight.py"
    )
    _MODNAME = "spec_pipeline_preflight_under_test"
    _spec = importlib.util.spec_from_file_location(_MODNAME, _PREFLIGHT_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    # Register before exec so dataclasses in the module can resolve their own
    # module via sys.modules (module_from_spec alone does not register it).
    sys.modules[_MODNAME] = _mod
    _spec.loader.exec_module(_mod)
    Preflight_Checker = _mod.Preflight_Checker
    PreflightResult = _mod.PreflightResult
    ToolResult = _mod.ToolResult
    required_tools_for_stages = _mod.required_tools_for_stages
    stages_requiring_llm = _mod.stages_requiring_llm
    UNKNOWN_VERSION = _mod.UNKNOWN_VERSION
    REDACTED = _mod.REDACTED


# ---------------------------------------------------------------------------
# Hermetic "tools absent" isolation  (Requirement 10.2)
# ---------------------------------------------------------------------------


def _force_tools_absent(monkeypatch, tmp_path) -> dict:
    """Force every binary tool to resolve as ABSENT, hermetically.

    The checker searches the interpreter's own ``bin`` directory (where the
    venv's ``slither``/``solc`` live) BEFORE ``PATH``, so simply emptying
    ``PATH`` is not enough. We monkeypatch ``Preflight_Checker._is_runnable`` to
    report nothing as runnable (covering the interpreter-dir search, the ``PATH``
    search, and any absolute-path probing alike) and return an env with an empty
    ``PATH`` pointed at an empty temp dir. ``monkeypatch`` auto-reverts, so no
    global state leaks to other tests.

    Returns an env dict suitable for ``Preflight_Checker(env=...)``.
    """
    empty_dir = tmp_path / "empty_bin"
    empty_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        Preflight_Checker, "_is_runnable", staticmethod(lambda candidate: False)
    )
    return {"PATH": ""}


# ---------------------------------------------------------------------------
# required_tools_for_stages / stages_requiring_llm  (Requirement 6.8)
# ---------------------------------------------------------------------------


def test_stage1_requires_slither_and_solc():
    req = required_tools_for_stages([1])
    assert set(req) == {"slither", "solc"}
    assert req["slither"] == [1]
    assert req["solc"] == [1]


def test_stage5_requires_certorarun_and_solc():
    req = required_tools_for_stages([5])
    assert set(req) == {"certoraRun", "solc"}
    assert req["certoraRun"] == [5]
    assert req["solc"] == [5]


def test_solc_blocks_both_stage1_and_stage5():
    req = required_tools_for_stages([1, 5])
    assert req["solc"] == [1, 5]


def test_llm_stages_are_2_3_4():
    assert stages_requiring_llm([1, 2, 3, 4, 5]) == [2, 3, 4]
    assert stages_requiring_llm([1, 5]) == []


def test_llm_only_stages_need_no_binary_tools():
    # Stages 2-4 alone require no filesystem binaries (R6.8).
    assert required_tools_for_stages([2, 3, 4]) == {}


# ---------------------------------------------------------------------------
# Absent binary tools resolve with correct blocked stages  (Requirements 6.1, 6.3)
# ---------------------------------------------------------------------------


def test_missing_tools_are_absent_with_blocked_stages(monkeypatch, tmp_path):
    # Force the binary tools absent hermetically (R10.2): the venv ships
    # slither/solc, so we cannot rely on the host lacking them.
    env = _force_tools_absent(monkeypatch, tmp_path)
    checker = Preflight_Checker(
        stages=[1, 2, 3, 4, 5], allow_missing_tools=False, env=env
    )
    result = checker.run()

    slither = result.tools["slither"]
    solc = result.tools["solc"]
    certora = result.tools["certoraRun"]

    assert slither.present is False
    assert solc.present is False
    assert certora.present is False

    assert slither.blocked_stages == [1]
    assert solc.blocked_stages == [1, 5]
    assert certora.blocked_stages == [5]

    # Each absent tool names its install command (R6.3).
    assert slither.install_command
    assert solc.install_command
    assert certora.install_command

    # Absent tools carry the searched locations (R6.3) and unknown version.
    assert slither.searched
    assert slither.version == UNKNOWN_VERSION


def test_interpreter_always_present_and_recorded():
    checker = Preflight_Checker(stages=[1], allow_missing_tools=False)
    result = checker.run()
    py = result.tools["python"]
    assert py.present is True
    assert py.path
    assert py.blocked_stages == []


def test_solc_not_blocked_when_not_requested():
    # Requesting only LLM stages: solc/slither/certoraRun still resolved for the
    # manifest but block nothing.
    checker = Preflight_Checker(stages=[2, 3], allow_missing_tools=False)
    result = checker.run()
    assert result.tools["solc"].blocked_stages == []
    assert result.tools["slither"].blocked_stages == []
    assert result.tools["certoraRun"].blocked_stages == []


# ---------------------------------------------------------------------------
# Interpreter-dir-before-PATH ordering  (Requirement 6.2)
# ---------------------------------------------------------------------------


def _make_fake_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\necho fake 1.2.3\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def test_interpreter_dir_searched_before_path(tmp_path, monkeypatch):
    # Put a fake "certoraRun" on PATH only. Because the interpreter dir is
    # searched first and holds no such binary, the PATH copy is what gets found,
    # and the searched list records the interpreter dir first.
    #
    # Hermetic isolation (R10.2): the venv now ships a REAL certoraRun in the
    # interpreter bin dir (searched first), which would otherwise shadow the
    # PATH fake and break the "found on PATH" leg. We constrain runnability to
    # THIS test's fake exe only, so the interpreter-dir search finds nothing
    # (regardless of what is installed there) and the PATH fake is what wins —
    # preserving the test's intent (prove interpreter-dir-before-PATH ordering)
    # while making it independent of the venv's installed tools. monkeypatch
    # auto-reverts, so the constraint leaks to no other test.
    path_dir = tmp_path / "pathbin"
    fake = _make_fake_exe(path_dir, "certoraRun")

    monkeypatch.setattr(
        Preflight_Checker,
        "_is_runnable",
        staticmethod(lambda candidate: str(candidate) == str(fake)),
    )

    fake_env = {"PATH": str(path_dir), "LLM_BASE_URL": "x", "LLM_MODEL": "y"}
    checker = Preflight_Checker(stages=[5], allow_missing_tools=False, env=fake_env)
    result = checker.run()

    certora = result.tools["certoraRun"]
    interp_dir = str(Path(os.sys.executable).parent)
    # Interpreter directory is the first entry searched (R6.2).
    assert certora.searched[0] == interp_dir
    # The PATH directory is searched after the interpreter dir.
    assert str(path_dir) in certora.searched
    assert certora.searched.index(interp_dir) < certora.searched.index(str(path_dir))
    # The fake on PATH is found (nothing in the interpreter dir shadows it).
    assert certora.present is True
    assert certora.path == str(fake)


def test_interpreter_dir_binary_wins_over_path(tmp_path):
    # A binary present in BOTH the interpreter dir and a PATH dir must resolve
    # to the interpreter-dir copy (searched first -> selected first, R6.2).
    interp_dir = Path(os.sys.executable).parent
    unique = "solc_preflight_probe_marker"
    interp_copy = _make_fake_exe(interp_dir, unique)
    try:
        path_dir = tmp_path / "pathbin"
        _make_fake_exe(path_dir, unique)

        fake_env = {"PATH": str(path_dir)}
        checker = Preflight_Checker(stages=[1], allow_missing_tools=False, env=fake_env)
        found, searched = checker._find_all(unique)
        assert found[0] == str(interp_copy)
    finally:
        interp_copy.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Version probe  (Requirement 6.1)
# ---------------------------------------------------------------------------


def test_version_unknown_when_probe_yields_no_number(tmp_path):
    # A fake executable that prints no version number -> "unknown".
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "toolx"
    exe.write_text("#!/bin/sh\necho no version here\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    checker = Preflight_Checker(stages=[1], allow_missing_tools=False)
    assert checker._probe_version(str(exe)) == UNKNOWN_VERSION


def test_version_parsed_when_present(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "tooly"
    exe.write_text("#!/bin/sh\necho 'tooly version 0.8.19'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    checker = Preflight_Checker(stages=[1], allow_missing_tools=False)
    assert checker._probe_version(str(exe)) == "0.8.19"


# ---------------------------------------------------------------------------
# Decision helpers: tool_unavailable / skipped_missing_tool  (R6.4, R6.5)
# ---------------------------------------------------------------------------


def test_missing_tool_without_flag_is_tool_unavailable_exit_5(monkeypatch, tmp_path):
    env = _force_tools_absent(monkeypatch, tmp_path)
    checker = Preflight_Checker(
        stages=[1, 2, 3, 4, 5], allow_missing_tools=False, env=env
    )
    result = checker.run()
    decision = checker.decide(result)
    assert decision["outcome"] == "tool_unavailable"
    assert decision["exit_code"] == 5
    names = {m["name"] for m in decision["missing_tools"]}
    assert {"slither", "solc", "certoraRun"} <= names
    # The decision carries blocked stages, searched locations, and install cmd.
    for m in decision["missing_tools"]:
        assert "blocked_stages" in m and "searched" in m and m["install_command"]


def test_missing_tool_with_flag_skips_per_stage(monkeypatch, tmp_path):
    env = _force_tools_absent(monkeypatch, tmp_path)
    checker = Preflight_Checker(
        stages=[1, 2, 3, 4, 5], allow_missing_tools=True, env=env
    )
    result = checker.run()
    decision = checker.decide(result)
    assert decision["outcome"] == "proceed"
    assert decision["exit_code"] == 0
    assert decision["skipped_outcome"] == "skipped_missing_tool"
    # Stage 1 blocked by slither/solc; stage 5 blocked by certoraRun/solc.
    assert 1 in decision["skipped"]
    assert 5 in decision["skipped"]
    # LLM-only stages are not blocked by a missing binary.
    assert 2 not in decision["skipped"]
    assert 3 not in decision["skipped"]
    assert 4 not in decision["skipped"]


# ---------------------------------------------------------------------------
# LLM probe  (Requirements 6.7, 6.9)
# ---------------------------------------------------------------------------


def test_llm_probe_not_run_without_llm_stage():
    # No LLM stage requested -> no probe (R6.9).
    calls = []

    def probe(base_url, model):
        calls.append((base_url, model))
        return None

    checker = Preflight_Checker(stages=[1, 5], allow_missing_tools=True, llm_probe=probe)
    result = checker.run()
    assert calls == []
    assert result.llm.probed is False


def test_llm_probe_runs_once_for_llm_stage():
    calls = []

    def probe(base_url, model):
        calls.append((base_url, model))
        return None

    checker = Preflight_Checker(stages=[2, 3, 4], allow_missing_tools=True, llm_probe=probe)
    result = checker.run()
    assert len(calls) == 1  # at most one probe (R6.9)
    assert result.llm.probed is True
    assert result.llm.available is True


def test_llm_unavailable_reports_url_model_key_redacted():
    def probe(base_url, model):
        return "401 Unauthorized: invalid api key sk-secret-123"

    fake_env = {
        "LLM_BASE_URL": "https://llm.example/v1",
        "LLM_MODEL": "gpt-x",
        "LLM_API_KEY": "sk-secret-123",
    }
    checker = Preflight_Checker(
        stages=[2, 3], allow_missing_tools=False, llm_probe=probe, env=fake_env
    )
    result = checker.run()
    assert result.llm.available is False
    assert result.llm.base_url == "https://llm.example/v1"
    assert result.llm.model == "gpt-x"
    # The API key value must not leak into the recorded reason (R6.7).
    assert "sk-secret-123" not in (result.llm.reason or "")
    assert REDACTED in (result.llm.reason or "")
    # Manifest rendering redacts the key entirely.
    assert result.llm.to_manifest()["api_key"] == REDACTED

    decision = checker.decide(result)
    assert decision["outcome"] == "llm_unavailable"
    assert decision["exit_code"] == 10
    assert decision["base_url"] == "https://llm.example/v1"
    assert decision["model"] == "gpt-x"
    assert decision["api_key"] == REDACTED


def test_llm_unavailable_on_timeout_reason():
    def probe(base_url, model):
        return "request timed out after 30s"

    fake_env = {"LLM_BASE_URL": "u", "LLM_MODEL": "m"}
    checker = Preflight_Checker(
        stages=[3], allow_missing_tools=True, llm_probe=probe, env=fake_env
    )
    result = checker.run()
    decision = checker.decide(result)
    # llm_unavailable takes precedence even with --allow-missing-tools.
    assert decision["outcome"] == "llm_unavailable"
    assert decision["exit_code"] == 10


def test_default_probe_makes_no_network_call():
    # The default probe returns success without any network access (R6.9). The
    # session network guard would raise if a real connection were attempted.
    fake_env = {"LLM_BASE_URL": "http://unreachable.invalid/v1", "LLM_MODEL": "m"}
    checker = Preflight_Checker(stages=[2], allow_missing_tools=True, env=fake_env)
    result = checker.run()
    assert result.llm.probed is True
    assert result.llm.available is True


# ---------------------------------------------------------------------------
# Manifest rendering  (Requirement 6.1)
# ---------------------------------------------------------------------------


def test_to_manifest_shape():
    checker = Preflight_Checker(stages=[1, 2, 5], allow_missing_tools=False)
    result = checker.run()
    manifest = result.to_manifest()
    assert set(manifest) == {"requested_stages", "tools", "llm"}
    assert "python" in manifest["tools"]
    assert "slither" in manifest["tools"]
    assert "solc" in manifest["tools"]
    assert "certoraRun" in manifest["tools"]
    assert manifest["llm"]["api_key"] == REDACTED
