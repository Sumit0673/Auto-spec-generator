"""Unit tests for the KEYLESS local CVL typecheck (Stage 5).

Covers :func:`spec_pipeline.stage5_verify.local_typecheck`, the Java >=19
detection it relies on (:func:`_find_java_home`), and the output parser
(:func:`_parse_local_typecheck`). Every test is fully offline and hermetic: a
fake certoraRun subprocess runner replays FIXED captured output (the real
invalid-spec error text certoraRun prints), and Java detection is driven with
temp "JVM" directories + a fake ``java -version`` probe. No real certoraRun or
java is ever invoked, and CERTORAKEY is never read.

``spec_pipeline/__init__.py`` eagerly imports slither-backed stages, so
``stage5_verify.py`` is loaded directly via importlib (mirroring
``test_stage5_verify.py``).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

try:  # normal path once optional native deps are present
    from spec_pipeline import stage5_verify as S  # type: ignore
except Exception:  # pragma: no cover - fallback when slither absent
    _MOD_NAME = "spec_pipeline_stage5_verify_local_tc_under_test"
    _PATH = Path(__file__).resolve().parents[2] / "spec_pipeline" / "stage5_verify.py"
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _PATH)
    S = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = S
    _spec.loader.exec_module(S)


# ---------------------------------------------------------------------------
# Fixtures: real captured certoraRun output text
# ---------------------------------------------------------------------------

# The real error text certoraRun prints on an INVALID spec (keyless, Java 21):
INVALID_SPEC_OUTPUT = """\
INFO: Compiling SimpleVault.sol
CRITICAL: [main] ERROR ALWAYS - Found errors in SimpleVault.spec:
CRITICAL: [main] ERROR ALWAYS - Error in spec file (SimpleVault.spec:26:37): \
Variable `bool` has not been declared. Did you forget to use `sig:` for a \
method selector?
CVL syntax or type check failed, please fix the issue.
"""

# A VALID spec passes the local checks and then tries to reach the cloud, where a
# missing key stops it. Reaching the cloud step means the LOCAL typecheck passed.
VALID_SPEC_CLOUD_REACHED_OUTPUT = """\
INFO: Compiling SimpleVault.sol
INFO: CVL type checking passed
INFO: Connecting to server...
ERROR: You must provide a CERTORAKEY to submit a verification job.
"""


class _FakeTable:
    def __init__(self, contracts):
        self.contracts = contracts


class _FakeContract:
    def __init__(self, inheritance=None):
        self.inheritance = list(inheritance or [])


def _sol(tmp_path: Path) -> Path:
    p = tmp_path / "src" / "SimpleVault.sol"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("pragma solidity ^0.8.0;\ncontract SimpleVault {}\n")
    return p


def _runner_returning(stdout: str, stderr: str = "", returncode: int = 0):
    """A fake certoraRun runner that records argv/env and replays fixed output."""
    captured = {}

    def run(cmd, capture_output=True, text=True, timeout=None, cwd=None, env=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run, captured


# ---------------------------------------------------------------------------
# (1) local_typecheck parses invalid-spec output -> failed + structured errors
# ---------------------------------------------------------------------------


def test_local_typecheck_parses_invalid_spec_errors(tmp_path):
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})
    runner, captured = _runner_returning(INVALID_SPEC_OUTPUT, returncode=1)

    result = S.local_typecheck(
        src,
        "rule r { bool; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        java_home="/fake/jdk21",  # skip Java auto-detection
    )

    assert result["status"] == "typecheck_failed"
    assert result["typecheck_passed"] is False
    assert len(result["errors"]) == 1
    err = result["errors"][0]
    assert err["file"] == "SimpleVault.spec"
    assert err["line"] == 26
    assert err["col"] == 37
    assert "has not been declared" in err["message"]
    assert "sig:" in err["message"]
    # raw_tail carries the last output lines for the caller.
    assert "CVL syntax or type check failed" in result["raw_tail"]
    # The argv uses the contract name target and the JAVA_HOME env was injected.
    argv = captured["cmd"]
    assert argv[0] == "/fake/certoraRun"
    target = argv[argv.index("--verify") + 1]
    assert target.startswith("SimpleVault:")
    assert captured["env"]["JAVA_HOME"] == "/fake/jdk21"
    assert captured["env"]["PATH"].startswith(str(Path("/fake/jdk21") / "bin"))


# ---------------------------------------------------------------------------
# (2) local_typecheck parses a passing / cloud-reached output -> passed
# ---------------------------------------------------------------------------


def test_local_typecheck_passes_when_cloud_reached(tmp_path):
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})
    # Nonzero exit (missing key stops the cloud submit) but local checks passed.
    runner, _ = _runner_returning(VALID_SPEC_CLOUD_REACHED_OUTPUT, returncode=1)

    result = S.local_typecheck(
        src,
        "rule r { assert true; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        java_home="/fake/jdk21",
    )

    # A missing key AFTER local checks passed is NOT a typecheck failure.
    assert result["status"] == "typecheck_passed"
    assert result["typecheck_passed"] is True
    assert result["errors"] == []


def test_local_typecheck_passes_on_clean_zero_exit(tmp_path):
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})
    runner, _ = _runner_returning("INFO: CVL type checking passed\n", returncode=0)

    result = S.local_typecheck(
        src, "rule r { assert true; }", output_dir=tmp_path / "out",
        table=table, certora_bin="/fake/certoraRun", runner=runner,
        java_home="/fake/jdk21",
    )
    assert result["status"] == "typecheck_passed"
    assert result["typecheck_passed"] is True


# ---------------------------------------------------------------------------
# (3) _find_java_home selects a >=19 dir and skips <19
# ---------------------------------------------------------------------------


def _make_fake_jvm(root: Path, name: str) -> Path:
    home = root / name
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("#!/bin/sh\n")
    return home


def test_find_java_home_selects_ge19_and_skips_old(tmp_path, monkeypatch):
    jvm_root = tmp_path / "jvm"
    j17 = _make_fake_jvm(jvm_root, "java-17-openjdk")
    j21 = _make_fake_jvm(jvm_root, "java-21-amazon-corretto")

    # Report each fake java's version by its directory name.
    versions = {
        str(j17 / "bin" / "java"): '17.0.9',
        str(j21 / "bin" / "java"): '21.0.1',
    }

    def fake_java_runner(cmd, capture_output=True, text=True, timeout=None):
        ver = versions.get(cmd[0], "1.8.0")
        return subprocess.CompletedProcess(
            cmd, 0, "", f'openjdk version "{ver}"\n'
        )

    # Point candidate discovery at only our temp jvm root, no $JAVA_HOME.
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(
        S, "_candidate_java_homes", lambda: [j17, j21]
    )

    home = S._find_java_home(min_major=19, runner=fake_java_runner)
    assert home == str(j21)  # the >=19 runtime wins; java-17 is skipped

    # A threshold above both -> no runtime found.
    assert S._find_java_home(min_major=99, runner=fake_java_runner) is None


def test_java_major_version_parses_legacy_and_modern():
    def make(out):
        return lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", out)

    assert S._java_major_version("java", runner=make('version "21.0.1"')) == 21
    assert S._java_major_version("java", runner=make('version "1.8.0_392"')) == 8
    assert S._java_major_version("java", runner=make("no version here")) is None


# ---------------------------------------------------------------------------
# (4) typecheck_unavailable when no Java >=19 (and when certoraRun absent)
# ---------------------------------------------------------------------------


def test_typecheck_unavailable_when_no_java(tmp_path, monkeypatch):
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})

    def runner(*a, **k):
        raise AssertionError("certoraRun must not run when Java is unavailable")

    # No Java >=19 anywhere.
    monkeypatch.setattr(S, "_find_java_home", lambda *a, **k: None)

    result = S.local_typecheck(
        src, "rule r {}", output_dir=tmp_path / "out", table=table,
        certora_bin="/fake/certoraRun", runner=runner,
    )
    assert result["status"] == "typecheck_unavailable"
    assert result["typecheck_passed"] is False
    assert "Java" in result["reason"]


def test_typecheck_unavailable_when_certora_absent(tmp_path, monkeypatch):
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})

    def runner(*a, **k):
        raise AssertionError("certoraRun must not run when the binary is absent")

    # Force certoraRun absent hermetically (the venv may ship a real one).
    monkeypatch.setattr(S, "_find_certora_bin", lambda: None)

    result = S.local_typecheck(
        src, "rule r {}", output_dir=tmp_path / "out", table=table,
        certora_bin=None, runner=runner, java_home="/fake/jdk21",
    )
    assert result["status"] == "typecheck_unavailable"
    assert result["typecheck_passed"] is False
    assert "certoraRun" in result["reason"]
    assert result["searched_paths"]


# ---------------------------------------------------------------------------
# (5) setup_failed: certoraRun fails BEFORE the CVL typechecker
# ---------------------------------------------------------------------------

# The real error text certoraRun prints when the --verify target does not match
# any compiled contract (the observed multi-contract-file failure).
VERIFY_TARGET_MISMATCH_OUTPUT = """\
INFO: Compiling GovernorBravoInterfaces.sol
CRITICAL: [main] ERROR ALWAYS - 'verify' argument, \
GovernorBravoDelegateStorageV1, doesn't match any contract name
"""

# An argparse-style CLI argument error.
ARG_ERROR_OUTPUT = """\
usage: certoraRun [-h] ...
certoraRun: error: unrecognized arguments: --not-a-real-flag
"""

# A Solidity / crytic-compile compilation failure.
COMPILE_FAIL_OUTPUT = """\
INFO: Compiling Broken.sol
Error compiling Broken.sol
CompilerError: Expected ';' but got '}'
"""


def test_parse_setup_failed_on_verify_target_mismatch():
    result = S._parse_local_typecheck(
        VERIFY_TARGET_MISMATCH_OUTPUT, "", returncode=1
    )
    assert result["status"] == "setup_failed"
    assert result["typecheck_passed"] is False
    assert result["errors"] == []
    assert result["reason"]
    assert "doesn't match any contract name" in result["reason"]
    assert "GovernorBravoDelegateStorageV1" in result["reason"]


def test_parse_setup_failed_on_unrecognized_arguments():
    result = S._parse_local_typecheck(ARG_ERROR_OUTPUT, "", returncode=2)
    assert result["status"] == "setup_failed"
    assert result["errors"] == []
    assert "unrecognized arguments" in result["reason"]


def test_parse_setup_failed_on_compilation_failure():
    result = S._parse_local_typecheck(COMPILE_FAIL_OUTPUT, "", returncode=1)
    assert result["status"] == "setup_failed"
    assert result["errors"] == []
    assert result["reason"]
    # Matched a compile signature.
    assert "Error compiling" in result["reason"] or "CompilerError" in result["reason"]


def test_parse_cvl_error_wins_over_setup_marker_precedence():
    # A genuine CVL spec-file error present ALONGSIDE a setup marker must stay
    # classified as typecheck_failed (precedence), not setup_failed.
    mixed = (
        VERIFY_TARGET_MISMATCH_OUTPUT
        + "CRITICAL: [main] ERROR ALWAYS - Error in spec file "
        "(Foo.spec:12:3): Variable `x` has not been declared.\n"
        + "CVL syntax or type check failed, please fix the issue.\n"
    )
    result = S._parse_local_typecheck(mixed, "", returncode=1)
    assert result["status"] == "typecheck_failed"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["line"] == 12


def test_parse_clean_run_still_typecheck_passed():
    result = S._parse_local_typecheck(
        "INFO: CVL type checking passed\n", "", returncode=0
    )
    assert result["status"] == "typecheck_passed"
    assert result["typecheck_passed"] is True

    cloud = S._parse_local_typecheck(VALID_SPEC_CLOUD_REACHED_OUTPUT, "", returncode=1)
    assert cloud["status"] == "typecheck_passed"


def test_parse_unrecognized_nonzero_is_setup_failed_not_empty_typecheck():
    # A bare nonzero exit with nothing recognizable must NOT become a phantom
    # typecheck_failed with empty errors -- it is setup_failed with a reason.
    result = S._parse_local_typecheck("some noise\n", "", returncode=7)
    assert result["status"] == "setup_failed"
    assert result["errors"] == []
    assert "exited 7" in result["reason"]


# ---------------------------------------------------------------------------
# CERTORAKEY is never read by the local typecheck
# ---------------------------------------------------------------------------


def test_local_typecheck_ignores_certora_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CERTORAKEY", "should-never-be-used")
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})
    runner, captured = _runner_returning(INVALID_SPEC_OUTPUT, returncode=1)

    result = S.local_typecheck(
        src, "rule r { bool; }", output_dir=tmp_path / "out", table=table,
        certora_bin="/fake/certoraRun", runner=runner, java_home="/fake/jdk21",
    )
    # Result is driven purely by the typecheck output, not by the key presence.
    assert result["status"] == "typecheck_failed"
    # The key is not injected as a certoraRun arg.
    assert "should-never-be-used" not in " ".join(captured["cmd"])


# ---------------------------------------------------------------------------
# verify_with_prover wiring: run_local_typecheck gates the cloud verdict
# ---------------------------------------------------------------------------


def test_verify_with_prover_typecheck_failed_short_circuits(tmp_path):
    # When the always-on local typecheck FAILS, verify_with_prover returns a
    # typecheck_failed report carrying the parsed diagnostics and does NOT
    # produce a passing cloud verdict.
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})
    runner, _ = _runner_returning(INVALID_SPEC_OUTPUT, returncode=1)

    report = S.verify_with_prover(
        src,
        "rule r { bool; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        run_local_typecheck=True,
        java_home="/fake/jdk21",
    )

    assert report["status"] == "typecheck_failed"
    assert report["local_typecheck_passed"] is False
    assert report["typecheck_diagnostics"][0]["line"] == 26


def test_verify_with_prover_records_local_typecheck_passed(tmp_path):
    # When the local typecheck PASSES, the cloud verdict proceeds and the report
    # records that local checks passed.
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})

    calls = {"n": 0}

    def runner(cmd, capture_output=True, text=True, timeout=None, cwd=None, env=None):
        calls["n"] += 1
        # First call = local typecheck (passes); second = cloud verdict.
        if calls["n"] == 1:
            return subprocess.CompletedProcess(
                cmd, 0, VALID_SPEC_CLOUD_REACHED_OUTPUT, ""
            )
        return subprocess.CompletedProcess(cmd, 0, "Rule 'r' PASSED\n", "")

    report = S.verify_with_prover(
        src,
        "rule r { assert true; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        run_local_typecheck=True,
        java_home="/fake/jdk21",
    )

    assert report["local_typecheck_passed"] is True
    # The cloud verdict still drives the status (a passing rule -> verified).
    assert report["status"] == "verified"


# ---------------------------------------------------------------------------
# verify_with_prover(typecheck_only=True): the KEYLESS local typecheck is the
# ONLY verdict; the cloud rule-proof is NEVER invoked even with a key present.
# ---------------------------------------------------------------------------


def test_verify_typecheck_only_passes_and_never_calls_cloud(tmp_path, monkeypatch):
    """A clean local typecheck under typecheck_only yields status
    ``typecheck_passed`` and the cloud runner is invoked at most once (the local
    typecheck itself), never a second time for a cloud verdict -- even with a
    CERTORAKEY set."""
    monkeypatch.setenv("CERTORAKEY", "present-but-must-be-ignored")
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})

    calls = {"n": 0}

    def runner(cmd, capture_output=True, text=True, timeout=None, cwd=None, env=None):
        calls["n"] += 1
        # The local typecheck reaches the cloud step (key stops it) -> passed.
        return subprocess.CompletedProcess(
            cmd, 1, VALID_SPEC_CLOUD_REACHED_OUTPUT, ""
        )

    report = S.verify_with_prover(
        src,
        "rule r { assert true; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        typecheck_only=True,
        java_home="/fake/jdk21",
    )

    assert report["status"] == "typecheck_passed"
    assert report["local_typecheck_passed"] is True
    # No rule verdicts were produced (cloud proof deferred).
    assert report["rules"] == []
    # The cloud runner ran exactly once: the local typecheck. There was NO
    # second (cloud-verdict) invocation.
    assert calls["n"] == 1


def test_verify_typecheck_only_failed_short_circuits_no_cloud(tmp_path, monkeypatch):
    """A failing local typecheck under typecheck_only yields ``typecheck_failed``
    and the cloud verdict is never produced, even with a key present."""
    monkeypatch.setenv("CERTORAKEY", "present-but-ignored")
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})

    calls = {"n": 0}

    def runner(cmd, capture_output=True, text=True, timeout=None, cwd=None, env=None):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 1, INVALID_SPEC_OUTPUT, "")

    report = S.verify_with_prover(
        src,
        "rule r { bool; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        typecheck_only=True,
        java_home="/fake/jdk21",
    )

    assert report["status"] == "typecheck_failed"
    assert report["local_typecheck_passed"] is False
    assert report["typecheck_diagnostics"][0]["line"] == 26
    # Only the local typecheck ran; no cloud verdict call.
    assert calls["n"] == 1


def test_verify_typecheck_only_setup_failed_maps_to_setup_failed(tmp_path, monkeypatch):
    """certoraRun failing BEFORE the CVL typechecker maps to ``setup_failed``
    (distinct nonzero terminal), never a cloud verdict."""
    monkeypatch.setenv("CERTORAKEY", "present-but-ignored")
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})

    calls = {"n": 0}

    def runner(cmd, capture_output=True, text=True, timeout=None, cwd=None, env=None):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            cmd, 1, VERIFY_TARGET_MISMATCH_OUTPUT, ""
        )

    report = S.verify_with_prover(
        src,
        "rule r { assert true; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin="/fake/certoraRun",
        runner=runner,
        typecheck_only=True,
        java_home="/fake/jdk21",
    )

    assert report["status"] == "setup_failed"
    assert report["local_typecheck_passed"] is False
    assert calls["n"] == 1


def test_verify_typecheck_only_tool_unavailable_when_certora_absent(tmp_path, monkeypatch):
    """When certoraRun is absent, typecheck_only maps to ``tool_unavailable``
    and never attempts any cloud call."""
    monkeypatch.setenv("CERTORAKEY", "present-but-ignored")
    monkeypatch.setattr(S, "_find_certora_bin", lambda: None)
    src = _sol(tmp_path)
    table = _FakeTable({"SimpleVault": _FakeContract()})

    def runner(*a, **k):
        raise AssertionError("certoraRun must not run when the binary is absent")

    report = S.verify_with_prover(
        src,
        "rule r { assert true; }",
        output_dir=tmp_path / "out",
        table=table,
        certora_bin=None,
        runner=runner,
        typecheck_only=True,
        java_home="/fake/jdk21",
    )

    assert report["status"] == "tool_unavailable"
    assert report["local_typecheck_passed"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
