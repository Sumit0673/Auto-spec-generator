"""
Stage 5: Verifier (design: R8, R20).

Runs certoraRun on the generated spec and produces a Verification_Report that
honestly records what the prover concluded. The three defects this rewrite
fixes, in order:

1. **Remappings and solc never reached certoraRun.** The old code computed
   remappings via an ad-hoc ``node_modules`` root-walk and then discarded them,
   and it looked for a venv ``solc`` but the resolved compiler was never wired
   in. Now the caller supplies a :class:`~spec_pipeline.resolve.ProjectResolution`
   (or explicit remappings / solc path), and both reach the certoraRun argv;
   the subprocess cwd is the resolved project root (Requirement 8.1).

2. **The ``--verify`` target used the file / directory stem.** For a directory
   input the target was literally ``project:...`` and for a file it was the file
   stem, which is only sometimes the contract name. Now the target is built from
   a *contract name taken from the Stage 1 table* (Requirements 8.8-8.10, 8.13).

3. **A missing prover reported ``pass_rate: 0``.** That reads like a measured
   zero. Now an absent tool yields Verification_Status ``tool_unavailable`` with
   a *null* pass rate and null per-verdict counts, plus the searched locations
   (Requirement 20.4).

certoraRun machine-readable output is preferred when present, with text output
as a fallback; the parse source is recorded (Requirements 20.7, 20.8).

The honest-status enum (verified / verified_with_warnings / violated / vacuous /
timeout / typecheck_failed) is refined in task 7.2. This module deliberately
leaves a single seam for that work — :func:`_classify_status` — and otherwise
focuses on the four things this task owns: remappings + solc reaching
certoraRun, the contract-name target, parse-source recording, and the
``tool_unavailable`` null-rate fix.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# ``resolve`` is pure (no slither); importing it here keeps the Verifier able to
# accept a ProjectResolution without a hard import cycle through the package.
try:  # pragma: no cover - exercised indirectly
    from .resolve import ProjectResolution, SolcSelection
except Exception:  # pragma: no cover - direct-by-path loading in tests
    ProjectResolution = Any  # type: ignore
    SolcSelection = Any  # type: ignore


# A subprocess runner is injectable so tests can assert the built argv without a
# real binary. It mirrors the subset of subprocess.run we rely on.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class VerifyContractError(ValueError):
    """A named ``--verify-contract`` is absent from the Stage 1 table (R8.13)."""


def _find_certora_bin() -> str | None:
    """Find certoraRun binary: check the interpreter's bin dir first, then PATH."""
    venv_bin = Path(sys.executable).parent / "certoraRun"
    if venv_bin.is_file():
        return str(venv_bin)
    return shutil.which("certoraRun")


# ---------------------------------------------------------------------------
# Java detection for the keyless local CVL typecheck
# ---------------------------------------------------------------------------
#
# certoraRun runs a LOCAL CVL typecheck (compile the .sol + type/syntax check the
# spec) BEFORE it uploads to the Certora cloud. That local step needs NO
# CERTORAKEY but requires a Java >= 19 runtime. The machine default java is often
# too old (17), so ``local_typecheck`` locates a new-enough JDK and injects it
# into the certoraRun subprocess environment. A missing key never blocks the
# local typecheck -- it only blocks the CLOUD verdict, which is a separate
# concern handled by ``verify_with_prover``.

# Runner for ``java -version`` probes, injectable so Java detection is testable
# without a real java on the machine.
_JAVA_VERSION_RE = re.compile(r'version "?(\d+)(?:\.(\d+))?')


def _java_major_version(
    java_bin: str | Path,
    *,
    runner: Optional[Runner] = None,
) -> Optional[int]:
    """Return the major version reported by ``<java_bin> -version``, or None.

    ``java -version`` writes to stderr (historically) in either the legacy
    ``1.8.0_x`` shape or the modern ``19.0.1`` shape. We read the first version
    token and normalize ``1.N`` to ``N`` (so ``1.8`` -> 8) and ``N.x`` to ``N``.
    Any probe failure (binary absent, non-parseable output) returns None so the
    caller simply skips that candidate rather than crashing.
    """
    run = runner or subprocess.run
    try:
        result = run(
            [str(java_bin), "-version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (getattr(result, "stderr", "") or "") + (getattr(result, "stdout", "") or "")
    m = _JAVA_VERSION_RE.search(blob)
    if not m:
        return None
    first = int(m.group(1))
    if first == 1 and m.group(2) is not None:
        # Legacy "1.8" style -> major 8.
        return int(m.group(2))
    return first


def _candidate_java_homes() -> list[Path]:
    """Ordered candidate JAVA_HOME directories to probe for a >=19 runtime.

    ``$JAVA_HOME`` is tried first, then the JVM install roots under
    ``/usr/lib/jvm`` (and a couple of other common roots), ordered so that
    higher-numbered runtimes win: java-21 before java-20 before java-19, then any
    remaining entries. The ordering is a stable sort on a best-effort numeric key
    extracted from the directory name so ``java-21-amazon-corretto`` beats
    ``java-17-openjdk`` deterministically.
    """
    candidates: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        key = str(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    env_home = os.environ.get("JAVA_HOME")
    if env_home:
        _add(Path(env_home))

    jvm_roots = ["/usr/lib/jvm", "/usr/java", "/opt/java", "/Library/Java/JavaVirtualMachines"]

    def _name_version_key(name: str) -> int:
        m = re.search(r"(?:java|jdk|corretto|openjdk)[-_]?(\d+)", name)
        if m:
            return int(m.group(1))
        m2 = re.search(r"(\d+)", name)
        return int(m2.group(1)) if m2 else -1

    for root in jvm_roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        try:
            entries = sorted(root_path.iterdir())
        except OSError:  # pragma: no cover - defensive
            continue
        dirs = [e for e in entries if e.is_dir()]
        # Highest version-number first so java-21 wins over java-19/20/17.
        dirs.sort(key=lambda p: _name_version_key(p.name), reverse=True)
        for d in dirs:
            _add(d)
    return candidates


def _find_java_home(
    min_major: int = 19,
    *,
    runner: Optional[Runner] = None,
) -> Optional[str]:
    """Locate a JAVA_HOME whose ``bin/java`` reports major version >= *min_major*.

    Probes ``$JAVA_HOME`` first, then the JVM install roots (ordered so
    higher-numbered runtimes win), verifying each candidate by running
    ``bin/java -version`` and parsing the reported major version. Returns the
    first candidate that satisfies ``>= min_major``, or None when no such runtime
    is found. Never raises: a candidate whose probe fails is skipped.

    ``runner`` is injectable so tests can supply fake ``java -version`` output
    for temp "JVM" directories without a real java on the machine.
    """
    for home in _candidate_java_homes():
        java_bin = home / "bin" / "java"
        if not java_bin.is_file():
            continue
        major = _java_major_version(java_bin, runner=runner)
        if major is not None and major >= min_major:
            return str(home)
    return None


def _java19_env(java_home: str) -> dict:
    """Build a subprocess env with JAVA_HOME set and its bin prepended to PATH.

    The returned env is a copy of the current environment with ``JAVA_HOME``
    pointed at *java_home* and ``<java_home>/bin`` prepended to ``PATH`` so the
    certoraRun subprocess finds the new-enough ``java`` first. CERTORAKEY is
    never read or set here.
    """
    env = dict(os.environ)
    env["JAVA_HOME"] = java_home
    bin_dir = str(Path(java_home) / "bin")
    existing = env.get("PATH", "")
    env["PATH"] = bin_dir + (os.pathsep + existing if existing else "")
    return env


def _certora_search_locations() -> list[str]:
    """The locations searched for certoraRun, recorded when it is absent (R20.4)."""
    locations = [str(Path(sys.executable).parent / "certoraRun")]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            locations.append(str(Path(entry) / "certoraRun"))
    return locations


def verify_with_prover(
    source_path: str | Path,
    cvl_spec: str,
    output_dir: str | Path | None = None,
    certora_args: list[str] | None = None,
    *,
    table: Any | None = None,
    contract_names: Optional[Iterable[str]] = None,
    verify_contracts: Optional[Iterable[str]] = None,
    resolution: Any | None = None,
    solc_path: str | Path | None = None,
    remappings: Optional[Iterable[str]] = None,
    project_root: str | Path | None = None,
    certora_bin: str | None = None,
    runner: Optional[Runner] = None,
    run_id: str | None = None,
    run_local_typecheck: bool = False,
    typecheck_only: bool = False,
    local_typecheck_runner: Optional[Runner] = None,
    java_runner: Optional[Runner] = None,
    java_home: str | None = None,
) -> dict:
    """
    Stage 5: run the Certora Prover and produce a Verification_Report.

    Parameters
    ----------
    source_path, cvl_spec, output_dir, certora_args:
        As before. ``verify_with_prover`` still runs in place so imports resolve.
    table:
        The Stage 1 table (anything exposing a ``contracts`` mapping keyed by
        contract name). Used to derive the verification target contract name and
        to validate ``verify_contracts`` (Requirements 8.8-8.10, 8.13).
    contract_names:
        An explicit list of first-party contract names, an alternative to
        ``table`` when the caller already has the names.
    verify_contracts:
        Contract names from a future ``--verify-contract`` CLI flag. When absent
        and more than one first-party contract exists, the Verifier defaults to
        the contracts that no other first-party contract inherits from
        (Requirements 8.9, 8.10). Naming a contract not in the table is an error
        that names it and the available names, and certoraRun is not invoked
        (Requirement 8.13).
    resolution:
        A :class:`~spec_pipeline.resolve.ProjectResolution`. Its remappings and
        selected solc reach the certoraRun argv, and its project root becomes the
        subprocess cwd (Requirement 8.1).
    solc_path, remappings, project_root:
        Explicit overrides used when a full ``resolution`` is not supplied. These
        take precedence over the values derived from ``resolution``.
    certora_bin, runner:
        Injection seams for tests: a fake binary path and a fake subprocess
        runner so the built argv can be asserted without a real prover.
    run_id:
        The run identifier written into the Spec_Bundle header (R20.6). When
        absent the header records the run id as ``unknown``.
    run_local_typecheck:
        When True, run the keyless local CVL typecheck (:func:`local_typecheck`)
        FIRST. A typecheck failure returns a ``typecheck_failed`` report carrying
        the parsed diagnostics (mapping to the existing typecheck_failed outcome)
        without invoking the cloud verdict; a pass is recorded on the report so
        the cloud verdict (or its ``tool_unavailable`` absence when no key) also
        notes that local checks passed. Defaults to False so existing callers and
        their tests are unaffected.
    typecheck_only:
        When True, the KEYLESS local CVL typecheck is the ONLY verdict: this
        function runs :func:`local_typecheck` and returns its result mapped to a
        Verification_Report (status among ``typecheck_passed`` /
        ``typecheck_failed`` / ``setup_failed`` / ``tool_unavailable``) WITHOUT
        ever invoking the cloud rule-proof, even when a CERTORAKEY and a
        certoraRun binary are present. This backs ``--typecheck-only`` mode: the
        cloud proof is deferred until the spec typechecks clean. Implies the
        keyless local typecheck. Defaults to False so existing callers are
        unaffected.
    local_typecheck_runner, java_runner, java_home:
        Injection seams for the local typecheck: a certoraRun subprocess runner
        (defaults to ``runner``), a ``java -version`` probe runner, and a
        JAVA_HOME override, so the local typecheck is testable without a real
        certoraRun or java. CERTORAKEY is never read.

    Returns a Verification_Report dict. The legacy ``rules`` / ``summary`` keys
    are preserved for existing callers (pipeline.py, stage3_iterative.py); the
    new honest fields (``status``, ``parse_source``, ``invocation``,
    ``pass_rate``, ``searched_paths``) are added alongside.
    """
    source_path = Path(source_path).resolve()

    # --- resolve the verification target contract name(s) FIRST -------------
    # An unknown --verify-contract must error before certoraRun is invoked
    # (R8.13), so this precedes the tool-presence check.
    available = _available_contract_names(table, contract_names)
    targets = _select_verify_targets(
        available=available,
        verify_contracts=verify_contracts,
        table=table,
    )

    certora_bin = certora_bin or _find_certora_bin()

    # The set of rule names declared in the emitted spec. "verified" requires a
    # verdict for EVERY one of these (R20.2), so the classifier needs them.
    declared_rules = _declared_rule_names(cvl_spec)

    # --- typecheck-only mode: KEYLESS local typecheck is the ONLY verdict ---
    # Run the local CVL typecheck and return its result mapped to a
    # Verification_Report; the cloud rule-proof is NEVER invoked even when a
    # CERTORAKEY / certoraRun binary is present (``--typecheck-only``). The cloud
    # proof is deferred until the spec typechecks clean.
    if typecheck_only:
        return _typecheck_only_report(
            source_path=source_path,
            cvl_spec=cvl_spec,
            output_dir=output_dir,
            declared_rules=declared_rules,
            table=table,
            contract_names=contract_names,
            verify_contracts=verify_contracts,
            resolution=resolution,
            solc_path=solc_path,
            remappings=remappings,
            project_root=project_root,
            certora_bin=certora_bin,
            runner=local_typecheck_runner or runner,
            java_runner=java_runner,
            java_home=java_home,
            run_id=run_id,
        )

    if not certora_bin:
        searched = _certora_search_locations()
        print(
            "Warning: certoraRun not found in PATH or interpreter bin dir; "
            "recording tool_unavailable"
        )
        report = _tool_unavailable_report(searched)
        report["declared_rules"] = sorted(declared_rules)
        if output_dir is not None:
            _write_bundle_spec(
                Path(output_dir),
                source_path,
                cvl_spec,
                report,
                run_id,
            )
        return report

    if output_dir is None:
        output_dir = _default_output_dir()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    base_name = source_path.stem if source_path.is_file() else source_path.name

    # --- always-on keyless local CVL typecheck (opt-in via run_local_typecheck)
    # certoraRun runs a local typecheck (compile + CVL type/syntax check) BEFORE
    # any cloud upload; that step needs NO CERTORAKEY (only Java >=19). When the
    # caller enables it, run it FIRST: a typecheck failure is terminal and maps
    # to the existing ``typecheck_failed`` outcome (exit 6), carrying the parsed
    # diagnostics; a pass is recorded so the cloud verdict (or its absence) can
    # note that local checks passed.
    local_tc: Optional[dict] = None
    if run_local_typecheck:
        local_tc = local_typecheck(
            source_path,
            cvl_spec,
            output_dir,
            table=table,
            contract_names=contract_names,
            verify_contracts=verify_contracts,
            resolution=resolution,
            solc_path=solc_path,
            remappings=remappings,
            project_root=project_root,
            certora_bin=certora_bin,
            runner=local_typecheck_runner or runner,
            java_runner=java_runner,
            java_home=java_home,
        )
        if local_tc.get("status") == "typecheck_failed":
            report = _typecheck_failed_report(local_tc, declared_rules)
            _write_bundle_spec(output_dir, source_path, cvl_spec, report, run_id)
            out_file = output_dir / f"{base_name}_stage5_report.json"
            with open(out_file, "w") as f:
                json.dump(report, f, indent=2, default=str)
            return report

    spec_file = output_dir / f"{base_name}_verify.spec"
    spec_file.write_text(cvl_spec)
    spec_file_abs = spec_file.resolve()

    sol_files = _collect_sol_files(source_path)

    # --- gather remappings, solc, cwd from the resolution / overrides -------
    resolved_remappings = _resolve_remappings(remappings, resolution)
    resolved_solc = _resolve_solc(solc_path, resolution)
    cwd = _resolve_cwd(project_root, resolution, source_path)

    # --- build the certoraRun argv -----------------------------------------
    # Target: <ContractName>:<spec> for each selected contract (R8.8).
    cmd = _build_certora_argv(
        certora_bin,
        sol_files,
        targets,
        spec_file_abs,
        solc=resolved_solc,
        remappings=resolved_remappings,
        extra_args=["--optimistic_loop", "--loop_iter", "1"]
        + (list(certora_args) if certora_args else []),
    )

    print(f"Stage 5: Running certoraRun on {len(sol_files)} files...")
    print(f"  Target contracts: {', '.join(targets)}")
    print(f"  CWD: {cwd}")

    run = runner or subprocess.run
    try:
        result = run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(cwd),
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = "certoraRun timed out after 300s"
    except FileNotFoundError:
        stdout = ""
        stderr = f"certoraRun binary not found: {certora_bin}"

    report = _parse_certora_output(
        stdout, stderr, output_dir=output_dir, declared_rules=declared_rules
    )

    report["invocation"] = {
        "argv": cmd,
        "cwd": str(cwd),
        "solc": resolved_solc,
        "remappings": list(resolved_remappings),
        "certora_version": "unknown",
    }
    report["searched_paths"] = []
    report["verify_targets"] = list(targets)
    report["declared_rules"] = sorted(declared_rules)
    if local_tc is not None:
        # Local typecheck passed (or was unavailable); record it alongside the
        # cloud verdict without changing the cloud status.
        report["local_typecheck"] = local_tc
        report["local_typecheck_passed"] = bool(local_tc.get("typecheck_passed"))

    out_file = output_dir / f"{base_name}_stage5_report.json"
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Exported Stage 5 report to {out_file}")

    # Write the user-facing Spec_Bundle .spec with the status header (R20.6).
    _write_bundle_spec(output_dir, source_path, cvl_spec, report, run_id)

    return report


# ---------------------------------------------------------------------------
# Shared certoraRun argv builder (used by verify_with_prover + local_typecheck)
# ---------------------------------------------------------------------------


def _build_certora_argv(
    certora_bin: str,
    sol_files: list[str],
    targets: list[str],
    spec_file_abs: Path,
    *,
    solc: Optional[str] = None,
    remappings: Optional[Iterable[str]] = None,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    """Build the ``certoraRun <sol...> --verify <Contract>:<spec> ...`` argv.

    Factored out so both the cloud verifier (:func:`verify_with_prover`) and the
    keyless local typecheck (:func:`local_typecheck`) construct the target the
    same way (R8.8) instead of duplicating the target-selection wiring. The
    ``--verify`` target is ``<ContractName>:<spec>`` for every selected contract,
    the resolved solc reaches the argv via ``--solc``, and remappings via
    ``--packages`` (R8.1).
    """
    cmd: list[str] = [certora_bin] + list(sol_files)
    for name in targets:
        cmd.extend(["--verify", f"{name}:{spec_file_abs}"])
    if extra_args:
        cmd.extend(extra_args)
    if solc:
        cmd.extend(["--solc", solc])
    remaps = list(remappings or [])
    if remaps:
        cmd.append("--packages")
        cmd.extend(remaps)
    return cmd


# ---------------------------------------------------------------------------
# Keyless local CVL typecheck (design: keyless feedback signal)
# ---------------------------------------------------------------------------

# The spec-file diagnostic line certoraRun prints on a typecheck failure, e.g.
#   Error in spec file (Foo.spec:26:37): Variable `bool` has not been declared...
_SPEC_ERROR_RE = re.compile(
    r"Error in spec file \(([^:]+):(\d+):(\d+)\):\s*(.*)$"
)
# The marker certoraRun prints when the local CVL check fails.
_TYPECHECK_FAIL_MARKER = "CVL syntax or type check failed"
# ---------------------------------------------------------------------------
# Setup / compile / target-resolution failure markers (distinct from a CVL
# typecheck failure)
# ---------------------------------------------------------------------------
#
# certoraRun can fail BEFORE it ever reaches the CVL typechecker: an unknown or
# mismatched ``--verify`` target contract, a Solidity/crytic-compile compilation
# failure, or a bad CLI argument. These are SETUP/COMPILE/CONFIG failures, not
# CVL typecheck failures and not a pass. They must be classified distinctly so
# the keyless loop treats them honestly (not feedable as a CVL fix, not a clean
# pass) and surfaces the real reason instead of stalling on empty error sets.
#
# Kept conservative: only strings certoraRun / crytic-compile actually emit.
_VERIFY_TARGET_MISMATCH_MARKERS = (
    # e.g. "'verify' argument, GovernorBravoDelegateStorageV1, doesn't match any
    # contract name" -- the observed --verify target mismatch.
    "doesn't match any contract name",
    "does not match any contract",
)
_ARG_ERROR_MARKERS = (
    # argparse usage/argument errors from certoraRun's CLI.
    "unrecognized arguments",
    "error: argument",
)
_COMPILE_FAIL_MARKERS = (
    # Solidity / crytic-compile compilation failures.
    "Error compiling",
    "CompilerError",
    "Compilation failed",
)
# All setup/compile markers, checked together.
_SETUP_FAIL_MARKERS = (
    _VERIFY_TARGET_MISMATCH_MARKERS + _ARG_ERROR_MARKERS + _COMPILE_FAIL_MARKERS
)
# Markers indicating local checks passed and the run reached the cloud step,
# where a missing key stops it. Reaching any of these means the LOCAL typecheck
# passed (a missing key is a cloud-only concern).
_CLOUD_REACHED_MARKERS = (
    "CERTORAKEY",
    "Certora Key",
    "certora key",
    "Sending verification request",
    "https://prover.certora.com",
    "https://vaas-stg.certora.com",
    "Connecting to server",
    "You can follow up the status",
)


def local_typecheck(
    source_path: str | Path,
    cvl_spec: str,
    output_dir: str | Path | None = None,
    *,
    table: Any | None = None,
    contract_names: Optional[Iterable[str]] = None,
    verify_contracts: Optional[Iterable[str]] = None,
    resolution: Any | None = None,
    solc_path: str | Path | None = None,
    remappings: Optional[Iterable[str]] = None,
    project_root: str | Path | None = None,
    certora_bin: str | None = None,
    runner: Optional[Runner] = None,
    java_runner: Optional[Runner] = None,
    java_home: str | None = None,
    min_java_major: int = 19,
    raw_tail_lines: int = 40,
) -> dict:
    """Run certoraRun's KEYLESS local CVL typecheck and parse the result.

    certoraRun compiles the ``.sol`` and runs the CVL type/syntax checker locally
    BEFORE any cloud upload. That step needs NO CERTORAKEY but requires Java >=19,
    so this function auto-detects a new-enough JDK (:func:`_find_java_home`) and
    injects it into the subprocess environment. CERTORAKEY is never read or set;
    a missing key only blocks the cloud verdict, which this function treats as
    "local checks passed" when it is reached AFTER the typecheck.

    Parameters mirror :func:`verify_with_prover` for target selection (``table`` /
    ``contract_names`` / ``verify_contracts``) and toolchain wiring (``solc_path``
    / ``remappings`` / ``project_root`` / ``resolution``). ``runner`` (the
    certoraRun subprocess) and ``java_runner`` (the ``java -version`` probe) are
    injectable so tests assert the built argv and parse fixed outputs without a
    real certoraRun or java. ``java_home`` overrides Java auto-detection.

    Returns a dict::

        {
          "typecheck_passed": bool,
          "errors": [{"file", "line", "col", "message"}, ...],
          "raw_tail": "<last N lines of output>",
          "status": "typecheck_passed" | "typecheck_failed"
                    | "setup_failed" | "typecheck_unavailable",
          "invocation": {...},   # argv/cwd/solc/remappings/java_home
        }

    ``status`` is:

    * ``typecheck_unavailable`` -- certoraRun absent, or no Java >=19 found; the
      reason is recorded and the process never crashes.
    * ``setup_failed`` -- certoraRun failed BEFORE the CVL typechecker: an
      unknown/mismatched ``--verify`` target contract, a Solidity/crytic-compile
      compilation failure, or a bad CLI argument. This is NOT a CVL typecheck
      failure and NOT a pass; ``errors`` is empty and ``reason`` carries a short
      human-readable explanation (the matched line). The keyless loop treats it
      like ``typecheck_unavailable`` (not feedable as a CVL fix, not a clean
      pass) and surfaces the real reason.
    * ``typecheck_failed`` -- the CVL typechecker rejected the spec; ``errors``
      carries the parsed ``file:line:col: message`` diagnostics.
    * ``typecheck_passed`` -- local checks passed (certoraRun exited 0 through the
      local step, or reached the cloud-upload / missing-key step afterwards).
    """
    source_path = Path(source_path).resolve()

    # Resolve target contract(s) first; an unknown --verify-contract errors
    # before any invocation (R8.13), matching verify_with_prover.
    available = _available_contract_names(table, contract_names)
    targets = _select_verify_targets(
        available=available,
        verify_contracts=verify_contracts,
        table=table,
    )

    certora_bin = certora_bin or _find_certora_bin()
    if not certora_bin:
        searched = _certora_search_locations()
        return {
            "typecheck_passed": False,
            "errors": [],
            "raw_tail": "",
            "status": "typecheck_unavailable",
            "reason": (
                "certoraRun not found in PATH or interpreter bin dir; "
                "cannot run local CVL typecheck"
            ),
            "searched_paths": searched,
            "invocation": {},
        }

    resolved_java_home = java_home or _find_java_home(
        min_major=min_java_major, runner=java_runner
    )
    if not resolved_java_home:
        return {
            "typecheck_passed": False,
            "errors": [],
            "raw_tail": "",
            "status": "typecheck_unavailable",
            "reason": (
                f"no Java >= {min_java_major} runtime found; the local CVL "
                "typecheck requires a new-enough JDK (checked $JAVA_HOME and "
                "the JVM install roots)"
            ),
            "invocation": {},
        }

    # Materialize the spec so certoraRun can typecheck it.
    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        base_name = source_path.stem if source_path.is_file() else source_path.name
        spec_file = out / f"{base_name}_typecheck.spec"
    else:
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="cvl_typecheck_"))
        base_name = source_path.stem if source_path.is_file() else source_path.name
        spec_file = tmp_dir / f"{base_name}_typecheck.spec"
    spec_file.write_text(cvl_spec)
    spec_file_abs = spec_file.resolve()

    sol_files = _collect_sol_files(source_path)
    resolved_remappings = _resolve_remappings(remappings, resolution)
    resolved_solc = _resolve_solc(solc_path, resolution) or "solc"
    cwd = _resolve_cwd(project_root, resolution, source_path)

    cmd = _build_certora_argv(
        certora_bin,
        sol_files,
        targets,
        spec_file_abs,
        solc=resolved_solc,
        remappings=resolved_remappings,
    )

    env = _java19_env(resolved_java_home)

    run = runner or subprocess.run
    try:
        result = run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(cwd),
            env=env,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        returncode = getattr(result, "returncode", 0)
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = "certoraRun local typecheck timed out after 300s"
        returncode = 1
    except FileNotFoundError:
        stdout = ""
        stderr = f"certoraRun binary not found: {certora_bin}"
        returncode = 1

    parsed = _parse_local_typecheck(
        stdout, stderr, returncode, raw_tail_lines=raw_tail_lines
    )
    parsed["invocation"] = {
        "argv": cmd,
        "cwd": str(cwd),
        "solc": resolved_solc,
        "remappings": list(resolved_remappings),
        "java_home": resolved_java_home,
    }
    return parsed


def _parse_local_typecheck(
    stdout: str,
    stderr: str,
    returncode: int,
    *,
    raw_tail_lines: int = 40,
) -> dict:
    """Parse certoraRun local-typecheck output into a structured result.

    Recognizes the ``Error in spec file (X.spec:LINE:COL): MESSAGE`` diagnostics
    and the ``CVL syntax or type check failed`` failure marker. When the failure
    marker is present (or a spec-file error was parsed), the typecheck FAILED
    (``typecheck_failed``) -- this takes PRECEDENCE over any setup/compile marker,
    since a genuine CVL diagnostic means the typechecker actually ran.

    When there is NO CVL diagnostic but the output carries a SETUP/COMPILE/CONFIG
    failure signature (unknown/mismatched ``--verify`` target, Solidity/
    crytic-compile compilation error, or a bad CLI argument), the run failed
    BEFORE the CVL typechecker and the status is ``setup_failed`` with an empty
    ``errors`` list and a short ``reason`` extracted from the matched line. This
    is distinct from a CVL typecheck failure and from a pass: the keyless loop
    treats it like ``typecheck_unavailable`` (not feedable, not a clean pass) and
    surfaces the real reason instead of stalling on empty error sets.

    Otherwise, if certoraRun reached the cloud-upload / missing-key step or
    exited 0 through the local checks, the local typecheck PASSED (a missing key
    is a cloud-only concern, never a local-typecheck failure).

    Precedence for a nonzero exit with nothing recognized: rather than a phantom
    ``typecheck_failed`` with an empty ``errors`` list (which the loop cannot feed
    back as a CVL fix and which hides the real problem), classify it as
    ``setup_failed`` with a reason noting certoraRun exited before CVL
    typechecking. The invariant that matters: an empty-error "failure" is NEVER
    labeled ``typecheck_failed``.
    """
    combined = (stdout or "") + "\n" + (stderr or "")

    errors: list[dict] = []
    for line in combined.splitlines():
        m = _SPEC_ERROR_RE.search(line)
        if m:
            errors.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "col": int(m.group(3)),
                    "message": m.group(4).strip(),
                }
            )

    lines = combined.strip().splitlines()
    tail_lines = lines[-raw_tail_lines:]
    raw_tail = "\n".join(tail_lines)

    failed_marker = _TYPECHECK_FAIL_MARKER in combined
    cloud_reached = any(marker in combined for marker in _CLOUD_REACHED_MARKERS)

    # PRECEDENCE: a genuine CVL typecheck failure (spec-file errors or the CVL
    # fail marker) wins and stays ``typecheck_failed`` even if a setup/compile
    # marker also appears -- the typechecker demonstrably ran.
    if failed_marker or errors:
        return {
            "typecheck_passed": False,
            "errors": errors,
            "raw_tail": raw_tail,
            "status": "typecheck_failed",
        }

    if cloud_reached or returncode == 0:
        # Local checks passed; the run either exited cleanly through the local
        # step or moved on to the cloud (where a missing key stops it).
        return {
            "typecheck_passed": True,
            "errors": [],
            "raw_tail": raw_tail,
            "status": "typecheck_passed",
        }

    # No CVL diagnostic and did not reach the cloud / exit 0. Look for a
    # setup/compile/config failure signature (target mismatch, compile error,
    # bad argument) BEFORE the generic nonzero fallback.
    setup_reason = _find_setup_failure_reason(lines)
    if setup_reason is not None:
        return {
            "typecheck_passed": False,
            "errors": [],
            "raw_tail": raw_tail,
            "status": "setup_failed",
            "reason": setup_reason,
        }

    # Nonzero exit with nothing recognized: prefer ``setup_failed`` over a
    # phantom ``typecheck_failed`` with no errors, so the loop does not stall on
    # empty error sets. The raw tail is included for debuggability.
    return {
        "typecheck_passed": False,
        "errors": [],
        "raw_tail": raw_tail,
        "status": "setup_failed",
        "reason": (
            f"certoraRun exited {returncode} before CVL typechecking "
            "(no CVL diagnostics parsed)"
        ),
    }


def _find_setup_failure_reason(lines: list[str]) -> Optional[str]:
    """Return a short reason if any setup/compile failure marker is present.

    Scans *lines* for the first line containing a recognized SETUP/COMPILE/CONFIG
    failure signature (see ``_SETUP_FAIL_MARKERS``) and returns that line
    (stripped) as a human-readable reason. Returns None when no marker is found.
    """
    for line in lines:
        for marker in _SETUP_FAIL_MARKERS:
            if marker in line:
                return line.strip()
    return None


# ---------------------------------------------------------------------------
# Target contract selection (R8.8-R8.10, R8.13)
# ---------------------------------------------------------------------------


def _available_contract_names(
    table: Any | None, contract_names: Optional[Iterable[str]]
) -> list[str]:
    """Return the first-party contract names, from the table or an explicit list."""
    if contract_names is not None:
        return list(contract_names)
    if table is not None and hasattr(table, "contracts"):
        return list(table.contracts.keys())
    return []


def _inheritance_map(table: Any | None) -> dict[str, list[str]]:
    """Best-effort parent map from the table's contract entries.

    The Stage 1 ``FirstPartyContract`` does not currently carry an inheritance
    field, so this reads an ``inheritance`` attribute when a contract entry
    happens to expose one (e.g. a richer table passed by a future caller) and
    otherwise returns an empty map. When the map is empty, the default target
    selection falls back to every first-party contract — a safe superset that a
    caller can narrow with ``--verify-contract``.
    """
    parents: dict[str, list[str]] = {}
    if table is None or not hasattr(table, "contracts"):
        return parents
    for name, entry in table.contracts.items():
        inh = getattr(entry, "inheritance", None)
        if inh:
            parents[name] = list(inh)
    return parents


def _select_verify_targets(
    *,
    available: list[str],
    verify_contracts: Optional[Iterable[str]],
    table: Any | None,
) -> list[str]:
    """Choose the contract names to verify.

    * An explicit ``verify_contracts`` list is validated against ``available``;
      an unknown name raises :class:`VerifyContractError` naming it and the
      available names (R8.13) — the caller must not invoke certoraRun.
    * With no explicit list and a single first-party contract, that contract is
      the target.
    * With no explicit list and more than one contract, the default is every
      contract that no other first-party contract inherits from (R8.9, R8.10).
      When inheritance information is unavailable, this degrades to all
      contracts (a superset the operator can narrow).
    """
    if verify_contracts is not None:
        requested = list(verify_contracts)
        if not available:
            # No table to validate against; trust the caller's names.
            return requested
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise VerifyContractError(
                "unknown --verify-contract "
                f"{unknown!r}; available contracts: {sorted(available)!r}"
            )
        return requested

    if not available:
        # No table and no names supplied: preserve the historic single-target
        # behavior by deriving nothing here; caller-supplied source stem is not
        # used (that was the bug). Return an empty list so the argv carries no
        # bogus target; downstream classification will report zero rules.
        return []

    if len(available) == 1:
        return list(available)

    parents = _inheritance_map(table)
    if parents:
        inherited_from: set[str] = set()
        for child_parents in parents.values():
            inherited_from.update(child_parents)
        roots = [name for name in available if name not in inherited_from]
        if roots:
            return sorted(roots)
    # Inheritance unknown or every contract is inherited from: verify all.
    return sorted(available)


# ---------------------------------------------------------------------------
# Resolution / override plumbing (R8.1)
# ---------------------------------------------------------------------------


def _resolve_remappings(
    remappings: Optional[Iterable[str]], resolution: Any | None
) -> list[str]:
    if remappings is not None:
        return list(remappings)
    if resolution is not None and hasattr(resolution, "remapping_args"):
        try:
            return list(resolution.remapping_args())
        except Exception:  # pragma: no cover - defensive
            return []
    return []


def _resolve_solc(
    solc_path: str | Path | None, resolution: Any | None
) -> Optional[str]:
    if solc_path is not None:
        return str(solc_path)
    if resolution is not None:
        solc = getattr(resolution, "solc", None)
        if solc is not None:
            path = getattr(solc, "path", None)
            if path:
                return str(path)
    return None


def _resolve_cwd(
    project_root: str | Path | None, resolution: Any | None, source_path: Path
) -> Path:
    if project_root is not None:
        return Path(project_root)
    if resolution is not None:
        root = getattr(resolution, "project_root", None)
        if root:
            return Path(root)
    return source_path.parent if source_path.is_file() else source_path


def _collect_sol_files(source_path: Path) -> list[str]:
    """Collect .sol file paths (original locations, not copies)."""
    if source_path.is_file():
        return [str(source_path)]
    return [str(f) for f in sorted(source_path.rglob("*.sol"))]


# ---------------------------------------------------------------------------
# Output parsing (R20.7, R20.8)
# ---------------------------------------------------------------------------


def _parse_certora_output(
    stdout: str,
    stderr: str,
    output_dir: Path | None = None,
    declared_rules: Optional[Iterable[str]] = None,
) -> dict:
    """Parse certoraRun output, preferring machine-readable results over text.

    When a structured results file is present (``output_dir/verification_results.json``
    or an emv-style ``results.json``), verdicts are read from it and the parse
    source is recorded as ``structured`` (R20.7). Otherwise verdicts come from
    the text output and the parse source is ``text`` (R20.8).

    ``declared_rules`` is the set of rule names the emitted spec declares; the
    classifier requires a verdict for every one of them before it may report
    ``verified`` (R20.2). ``warnings`` are scraped from the prover output and
    carried through so ``verified`` vs ``verified_with_warnings`` is decidable.
    """
    structured = _read_structured_results(output_dir) if output_dir else None
    if structured is not None:
        rules = structured
        parse_source = "structured"
    else:
        rules = _parse_text_rules(stdout, stderr)
        parse_source = "text"

    warnings = _parse_warnings(stdout, stderr)
    typecheck = _parse_typecheck_diagnostics(stdout, stderr)

    summary = _summarize(rules)
    status = _classify_status(
        rules,
        tool_available=True,
        declared_rules=declared_rules,
        warnings=warnings,
        typecheck_diagnostics=typecheck,
    )

    report = {
        "status": status,
        "parse_source": parse_source,
        "rules": rules,
        "warnings": warnings,
        "summary": summary,
        "pass_rate": summary["pass_rate"],
        "raw_stdout": stdout[-5000:] if stdout else "",
        "raw_stderr": stderr[-5000:] if stderr else "",
    }
    if typecheck:
        report["typecheck_diagnostics"] = typecheck
    return report


def _read_structured_results(output_dir: Path) -> list[dict] | None:
    """Read verdicts from a certoraRun machine-readable results file, if any."""
    candidates = [
        output_dir / "verification_results.json",
        output_dir / "results.json",
        output_dir / "emv-output" / "results.json",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):  # pragma: no cover - defensive
            continue
        rules = _rules_from_structured(data)
        if rules is not None:
            return rules
    return None


def _rules_from_structured(data: Any) -> list[dict] | None:
    """Map a certoraRun results JSON object to the rule list shape we use.

    certoraRun's JSON shape has drifted across versions; this accepts the common
    ``{"rules": {name: verdict}}`` and ``{"rules": [{"name", "status"}]}`` forms.
    Returns None when the object holds no recognizable rule verdicts so the
    caller can fall back to text.
    """
    if not isinstance(data, dict):
        return None
    raw = data.get("rules")
    rules: list[dict] = []
    if isinstance(raw, dict):
        for name, verdict in sorted(raw.items()):
            rules.append({"name": name, "status": _normalize_verdict(str(verdict))})
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("rule")
            verdict = entry.get("status") or entry.get("result") or entry.get("verdict")
            if name is None or verdict is None:
                continue
            rules.append({"name": str(name), "status": _normalize_verdict(str(verdict))})
    else:
        return None
    return rules or None


def _normalize_verdict(text: str) -> str:
    t = text.strip().upper()
    mapping = {
        "SUCCESS": "PASSED",
        "PASS": "PASSED",
        "PASSED": "PASSED",
        "VERIFIED": "PASSED",
        "VIOLATED": "FAILED",
        "FAIL": "FAILED",
        "FAILED": "FAILED",
        "VACUOUS": "VACUOUS",
        "TIMEOUT": "TIMEOUT",
        "ERROR": "ERROR",
        "UNKNOWN": "ERROR",
    }
    return mapping.get(t, t)


def _parse_text_rules(stdout: str, stderr: str) -> list[dict]:
    """Parse certoraRun text output for rule statuses and vacuity."""
    combined = stdout + "\n" + stderr
    rules: list[dict] = []

    # The verdict for a rule is emitted on the SAME line as the rule name
    # (``Rule '<name>' VERDICT``). Match per-line and take the FIRST verdict
    # token after the rule name; do NOT use re.DOTALL. A cross-line ``.*?`` +
    # DOTALL would let the verdict group jump to a later line and match the
    # ordinary substring "error" inside a rule NAME (e.g. a rule literally
    # named ``error``), corrupting an unrelated rule's verdict (R20.9).
    #
    # ``[^'\n]*?`` allows trailing prose between the rule name and its verdict
    # on the same logical line (real certoraRun text can put the verdict after
    # the name) while ``[^'\n]`` forbids crossing a newline OR entering another
    # quoted name, so the verdict can never be captured from inside a quoted
    # rule name on a following line.
    rule_pattern = re.compile(
        r"Rule\s+'([^']+)'[^'\n]*?\b(PASSED|VACUOUS|FAILED|TIMEOUT|ERROR)\b",
        re.IGNORECASE,
    )
    for match in rule_pattern.finditer(combined):
        rules.append({"name": match.group(1), "status": match.group(2).upper()})

    vacuous_pattern = re.compile(
        r"(?:vacuous|vacuity).*?rule\s+'?([^'\s]+)'?", re.IGNORECASE
    )
    for match in vacuous_pattern.finditer(combined):
        rule_name = match.group(1)
        existing = next((r for r in rules if r["name"] == rule_name), None)
        if existing:
            existing["status"] = "VACUOUS"
            existing["vacuity_reason"] = "Prover reported vacuous"
        else:
            rules.append(
                {
                    "name": rule_name,
                    "status": "VACUOUS",
                    "vacuity_reason": "Prover reported vacuous",
                }
            )

    dead_pattern = re.compile(
        r"(?:dead code|unreachable|precondition unsatisfiable).*?rule\s+'?([^'\s]+)'?",
        re.IGNORECASE,
    )
    for match in dead_pattern.finditer(combined):
        rule_name = match.group(1)
        existing = next((r for r in rules if r["name"] == rule_name), None)
        if existing:
            existing["status"] = "DEAD"
            existing["dead_reason"] = "Precondition unsatisfiable"
        else:
            rules.append(
                {
                    "name": rule_name,
                    "status": "DEAD",
                    "dead_reason": "Precondition unsatisfiable",
                }
            )

    return rules


def _summarize(rules: list[dict]) -> dict:
    total = len(rules)
    passed = sum(1 for r in rules if r["status"] == "PASSED")
    vacuous = sum(1 for r in rules if r["status"] == "VACUOUS")
    failed = sum(1 for r in rules if r["status"] == "FAILED")
    dead = sum(1 for r in rules if r["status"] == "DEAD")
    timeout = sum(1 for r in rules if r["status"] == "TIMEOUT")
    return {
        "total_rules": total,
        "passed": passed,
        "vacuous": vacuous,
        "failed": failed,
        "dead": dead,
        "timeout": timeout,
        "pass_rate": passed / total if total > 0 else None,
    }


def _classify_status(
    rules: list[dict],
    *,
    tool_available: bool,
    declared_rules: Optional[Iterable[str]] = None,
    warnings: Optional[Iterable[str]] = None,
    typecheck_diagnostics: Optional[Iterable[Any]] = None,
) -> str:
    """Map prover results to exactly one honest Verification_Status.

    The status enum (design Data Models, R20.2, R20.3, R20.5, R20.11):

    * ``tool_unavailable`` — certoraRun was absent (``tool_available`` False).
    * ``typecheck_failed`` — the CVL typechecker rejected the spec: any recorded
      typechecker diagnostic, or any rule the prover marked ``ERROR`` (R20.5).
    * ``not_run`` — the prover produced no rule verdicts at all.
    * ``violated`` — at least one rule has a failing verdict (highest-severity
      verdict outcome).
    * ``timeout`` — no failure, but at least one rule timed out (the prover
      budget elapsed for it).
    * ``vacuous`` — every rule passes but at least one is vacuous/dead (R20.3).
    * ``verified`` — the prover returned a verdict for EVERY declared rule, the
      declared rule count is >= 1, every verdict is passing, there are zero
      vacuous rules, and zero warnings (R20.2, R20.11).
    * ``verified_with_warnings`` — as ``verified`` but with >= 1 prover warning.

    The honest invariant (R20.11) is enforced structurally here: the classifier
    only ever returns ``verified`` when it has confirmed rule_count >= 1, all
    passing, zero vacuous, and zero warnings — see :func:`is_verified_honest`.
    """
    if not tool_available:
        return "tool_unavailable"

    warnings = list(warnings or [])
    typecheck_diagnostics = list(typecheck_diagnostics or [])

    # A CVL typechecker rejection is terminal regardless of any rule list
    # (R20.5). Prover-reported ERROR verdicts are treated the same way.
    statuses = {r["status"] for r in rules}
    if typecheck_diagnostics or (statuses & {"ERROR"}):
        return "typecheck_failed"

    if not rules:
        return "not_run"

    # Verdict-severity ordering: a failing verdict dominates a timeout, which
    # dominates vacuity, which dominates a clean pass.
    if statuses & {"FAILED"}:
        return "violated"
    if statuses & {"TIMEOUT"}:
        return "timeout"
    if statuses & {"VACUOUS", "DEAD"}:
        return "vacuous"

    # Every rule passed. Only now can we consider (with)warnings/verified. A
    # missing verdict for any declared rule blocks ``verified`` (R20.2).
    all_passing = statuses == {"PASSED"}
    if all_passing and _every_declared_rule_has_verdict(rules, declared_rules):
        if warnings:
            return "verified_with_warnings"
        return "verified"

    # Passing rules but not a verdict for every declared rule: honest fallback.
    return "not_run"


def _every_declared_rule_has_verdict(
    rules: list[dict], declared_rules: Optional[Iterable[str]]
) -> bool:
    """True when every declared rule name has a verdict in ``rules`` (R20.2).

    When ``declared_rules`` is None the declared set is unknown, so we require
    at least one rule and treat the verdict rules themselves as the declared
    set — a conservative reading that never claims a verdict for a rule that was
    never run.
    """
    verdict_names = {r["name"] for r in rules}
    if declared_rules is None:
        return len(verdict_names) >= 1
    declared = set(declared_rules)
    if not declared:
        # No declared rules -> rule count is zero -> cannot be verified (R20.11).
        return False
    return declared.issubset(verdict_names)


def is_verified_honest(report: dict) -> bool:
    """The R20.11 invariant, checkable on a finished report.

    A ``verified`` status implies a rule count of at least one, a passing
    verdict for every rule, a Vacuous_Rule count of zero, and zero warnings.
    Returns True when the report is internally consistent with respect to this
    invariant (i.e. either it is not ``verified``, or it satisfies every clause).
    """
    if report.get("status") != "verified":
        return True
    rules = report.get("rules") or []
    if len(rules) < 1:
        return False
    if any(r.get("status") != "PASSED" for r in rules):
        return False
    if any(r.get("status") in {"VACUOUS", "DEAD"} for r in rules):
        return False
    if report.get("warnings"):
        return False
    return True


# ---------------------------------------------------------------------------
# Declared-rule and warning parsing (R20.2, R20.5)
# ---------------------------------------------------------------------------

_RULE_DECL = re.compile(r"\brule\s+(\w+)")
_INVARIANT_DECL = re.compile(r"\binvariant\s+(\w+)")


def _declared_rule_names(cvl_spec: str) -> set[str]:
    """Parse the rule and invariant names declared in the emitted spec.

    Uses simple ``rule <name>`` / ``invariant <name>`` regexes (R20.2). This is
    a syntactic scan, adequate because the classifier only needs the set of
    names for which a verdict is expected.
    """
    if not cvl_spec:
        return set()
    names: set[str] = set()
    names.update(_RULE_DECL.findall(cvl_spec))
    names.update(_INVARIANT_DECL.findall(cvl_spec))
    return names


_WARNING_LINE = re.compile(r"^\s*(?:\[?WARN(?:ING)?\]?|Warning)\b[:\s]*(.*)$", re.I)


def _parse_warnings(stdout: str, stderr: str) -> list[str]:
    """Scrape prover warning lines from the text output.

    A warning is any line beginning with a WARN/WARNING/Warning marker. The
    message text (with the marker stripped) is returned, deduplicated while
    preserving first-seen order.
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    seen: list[str] = []
    for line in combined.splitlines():
        m = _WARNING_LINE.match(line)
        if m:
            msg = m.group(1).strip() or line.strip()
            if msg not in seen:
                seen.append(msg)
    return seen


_TYPECHECK_LINE = re.compile(
    r"(?:CVL type|type)[- ]?check(?:er)?\s+(?:error|failed).*?(?:line\s+(\d+))?[:\s]*(.*)$",
    re.I,
)
_SYNTAX_ERROR_LINE = re.compile(
    r"(?:syntax error|Syntax error).*?line\s+(\d+)[:\s]*(.*)$", re.I
)


def _parse_typecheck_diagnostics(stdout: str, stderr: str) -> list[dict]:
    """Scrape CVL typechecker diagnostics, each with its line number (R20.5)."""
    combined = (stdout or "") + "\n" + (stderr or "")
    diags: list[dict] = []
    for line in combined.splitlines():
        for pattern in (_TYPECHECK_LINE, _SYNTAX_ERROR_LINE):
            m = pattern.search(line)
            if m:
                line_no = m.group(1)
                message = (m.group(2) or "").strip() or line.strip()
                diags.append(
                    {
                        "line": int(line_no) if line_no else None,
                        "message": message,
                    }
                )
                break
    return diags


# ---------------------------------------------------------------------------
# Spec_Bundle header (R20.6)
# ---------------------------------------------------------------------------


def vacuous_rule_names(report: dict) -> list[str]:
    """Names of the rules the prover reported as vacuous/dead (R20.3)."""
    return sorted(
        r["name"]
        for r in (report.get("rules") or [])
        if r.get("status") in {"VACUOUS", "DEAD"}
    )


def _verdict_counts(report: dict) -> dict[str, int]:
    """Count of rules per verdict, for the Spec_Bundle header (R20.6)."""
    counts: dict[str, int] = {}
    for r in report.get("rules") or []:
        verdict = str(r.get("status", "UNKNOWN"))
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def spec_bundle_header(report: dict, run_id: str | None) -> str:
    """Return the CVL comment header prepended to the emitted ``.spec`` (R20.6).

    The header records the Verification_Status, the run identifier, the
    certoraRun version, and the count of rules per verdict. Where no certoraRun
    invocation occurred (``tool_unavailable`` / no invocation), the version is
    marked unavailable and the per-verdict counts are omitted rather than
    written as zeros.
    """
    status = report.get("status", "not_run")
    run_identifier = run_id or "unknown"
    invocation = report.get("invocation") or {}
    version = invocation.get("certora_version") or "unavailable"

    lines = [
        "// ---- Spec_Bundle ----",
        f"// Verification_Status: {status}",
        f"// Run_Id: {run_identifier}",
        f"// certoraRun_version: {version}",
    ]

    tool_ran = bool(invocation.get("argv")) and version not in {
        "unavailable",
        None,
    }
    if tool_ran:
        counts = _verdict_counts(report)
        if counts:
            rendered = ", ".join(f"{v}={n}" for v, n in sorted(counts.items()))
        else:
            rendered = "(none)"
        lines.append(f"// Rules_per_verdict: {rendered}")
        vacuous = vacuous_rule_names(report)
        if vacuous:
            lines.append(f"// Vacuous_Rules: {', '.join(vacuous)}")
    else:
        # No invocation: omit per-verdict counts rather than writing zeros.
        lines.append("// Rules_per_verdict: unavailable (certoraRun not invoked)")

    lines.append("// ----------------------")
    return "\n".join(lines) + "\n"


def _write_bundle_spec(
    output_dir: Path,
    source_path: Path,
    cvl_spec: str,
    report: dict,
    run_id: str | None,
) -> Path:
    """Write the final ``.spec`` with the Spec_Bundle header prepended (R20.6)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = source_path.stem if source_path.is_file() else source_path.name
    bundle_file = output_dir / f"{base_name}.spec"
    header = spec_bundle_header(report, run_id)
    bundle_file.write_text(header + "\n" + cvl_spec)
    report["bundle_spec_path"] = str(bundle_file)
    return bundle_file


# ---------------------------------------------------------------------------
# tool_unavailable report (R20.4)
# ---------------------------------------------------------------------------


def _default_output_dir() -> Path:
    """The default Stage 5 output directory: ``spec_pipeline/Output``.

    Resolved relative to this package so it is stable regardless of the process
    cwd. This mirrors the pipeline default and keeps run output out of the repo
    root.
    """
    return Path(__file__).resolve().parent / "Output"


def _typecheck_passed_report(local_tc: dict, declared_rules: Iterable[str]) -> dict:
    """Build a ``typecheck_passed`` Verification_Report from a clean local check.

    Used by ``--typecheck-only`` mode: the keyless local CVL typecheck accepted
    the spec and the cloud rule-proof is deferred, so the terminal status is
    ``typecheck_passed`` (a SUCCESS, exit 0). No rule verdicts are produced (the
    cloud proof was never run), so the per-verdict counts are zero and the pass
    rate is null.
    """
    return {
        "status": "typecheck_passed",
        "parse_source": "local_typecheck",
        "rules": [],
        "warnings": [],
        "summary": {
            "total_rules": 0,
            "passed": 0,
            "vacuous": 0,
            "failed": 0,
            "dead": 0,
            "timeout": 0,
            "pass_rate": None,
        },
        "pass_rate": None,
        "searched_paths": [],
        "local_typecheck": local_tc,
        "local_typecheck_passed": True,
        "declared_rules": sorted(declared_rules),
        "invocation": local_tc.get("invocation", {}),
        "raw_tail": local_tc.get("raw_tail", ""),
        "note": (
            "keyless local CVL typecheck passed clean; cloud rule-proof deferred "
            "(--typecheck-only)"
        ),
    }


def _setup_failed_report(local_tc: dict, declared_rules: Iterable[str]) -> dict:
    """Build a ``setup_failed`` Verification_Report from a local check.

    certoraRun failed BEFORE the CVL typechecker (unknown/mismatched ``--verify``
    target, compile error, bad argument). This is a distinct nonzero terminal
    (exit 13), NOT a CVL typecheck failure and NOT a pass.
    """
    reason = local_tc.get("reason", "certoraRun setup/compile failure")
    return {
        "status": "setup_failed",
        "parse_source": "local_typecheck",
        "rules": [],
        "warnings": [],
        "summary": {
            "total_rules": None,
            "passed": None,
            "vacuous": None,
            "failed": None,
            "dead": None,
            "timeout": None,
            "pass_rate": None,
        },
        "pass_rate": None,
        "searched_paths": local_tc.get("searched_paths", []),
        "local_typecheck": local_tc,
        "local_typecheck_passed": False,
        "declared_rules": sorted(declared_rules),
        "invocation": local_tc.get("invocation", {}),
        "raw_tail": local_tc.get("raw_tail", ""),
        "setup_failed_reason": reason,
        "note": f"keyless local CVL typecheck could not run: {reason}",
    }


def _typecheck_only_report(
    *,
    source_path: Path,
    cvl_spec: str,
    output_dir: str | Path | None,
    declared_rules: Iterable[str],
    table: Any | None,
    contract_names: Optional[Iterable[str]],
    verify_contracts: Optional[Iterable[str]],
    resolution: Any | None,
    solc_path: str | Path | None,
    remappings: Optional[Iterable[str]],
    project_root: str | Path | None,
    certora_bin: str | None,
    runner: Optional[Runner],
    java_runner: Optional[Runner],
    java_home: str | None,
    run_id: str | None,
) -> dict:
    """Run the keyless local typecheck and map it to a Verification_Report.

    The ``--typecheck-only`` terminal: the local CVL typecheck is the ONLY
    verdict and the cloud rule-proof is never invoked. The local status maps to:

    * ``typecheck_passed``      -> ``typecheck_passed`` (exit 0, success)
    * ``typecheck_failed``      -> ``typecheck_failed`` (exit 6)
    * ``setup_failed``          -> ``setup_failed`` (exit 13)
    * ``typecheck_unavailable`` -> ``tool_unavailable`` (exit 5)

    The report is persisted (report JSON + Spec_Bundle header) when an
    ``output_dir`` is available, exactly like the cloud path.
    """
    local_tc = local_typecheck(
        source_path,
        cvl_spec,
        output_dir,
        table=table,
        contract_names=contract_names,
        verify_contracts=verify_contracts,
        resolution=resolution,
        solc_path=solc_path,
        remappings=remappings,
        project_root=project_root,
        certora_bin=certora_bin,
        runner=runner,
        java_runner=java_runner,
        java_home=java_home,
    )
    status = local_tc.get("status")
    if status == "typecheck_failed":
        report = _typecheck_failed_report(local_tc, declared_rules)
    elif status == "typecheck_passed":
        report = _typecheck_passed_report(local_tc, declared_rules)
    elif status == "setup_failed":
        report = _setup_failed_report(local_tc, declared_rules)
    else:
        # typecheck_unavailable (certoraRun absent or no Java >=19): map to the
        # existing tool_unavailable terminal so no cloud proof is ever implied.
        report = _tool_unavailable_report(local_tc.get("searched_paths", []))
        report["declared_rules"] = sorted(declared_rules)
        report["local_typecheck"] = local_tc
        report["local_typecheck_passed"] = False
        if local_tc.get("reason"):
            report["note"] = local_tc["reason"]

    # Persist alongside the cloud path when an output dir is available.
    if output_dir is not None:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        base_name = (
            source_path.stem if source_path.is_file() else source_path.name
        )
        _write_bundle_spec(out_dir, source_path, cvl_spec, report, run_id)
        out_file = out_dir / f"{base_name}_stage5_report.json"
        with open(out_file, "w") as f:
            json.dump(report, f, indent=2, default=str)
    return report


def _typecheck_failed_report(local_tc: dict, declared_rules: Iterable[str]) -> dict:
    """Build a ``typecheck_failed`` Verification_Report from a local typecheck.

    The keyless local CVL typecheck rejected the spec; the report records the
    parsed ``file:line:col: message`` diagnostics as typecheck diagnostics (so it
    maps to the existing ``typecheck_failed`` outcome / exit 6) and carries the
    full local-typecheck result for the caller.
    """
    diagnostics = [
        {
            "file": e.get("file"),
            "line": e.get("line"),
            "col": e.get("col"),
            "message": e.get("message"),
        }
        for e in local_tc.get("errors", [])
    ]
    return {
        "status": "typecheck_failed",
        "parse_source": "local_typecheck",
        "rules": [],
        "warnings": [],
        "summary": {
            "total_rules": 0,
            "passed": 0,
            "vacuous": 0,
            "failed": 0,
            "dead": 0,
            "timeout": 0,
            "pass_rate": None,
        },
        "pass_rate": None,
        "searched_paths": [],
        "typecheck_diagnostics": diagnostics,
        "local_typecheck": local_tc,
        "local_typecheck_passed": False,
        "declared_rules": sorted(declared_rules),
        "invocation": local_tc.get("invocation", {}),
        "raw_tail": local_tc.get("raw_tail", ""),
    }


def _tool_unavailable_report(searched_paths: list[str]) -> dict:
    """Report emitted when certoraRun is absent.

    Verification_Status is ``tool_unavailable``; the pass rate and every
    per-verdict count are null (not zero) so the report never reads like a
    measured zero; the searched locations are recorded (R20.4).
    """
    return {
        "status": "tool_unavailable",
        "parse_source": None,
        "rules": [],
        "warnings": [],
        "summary": {
            "total_rules": None,
            "passed": None,
            "vacuous": None,
            "failed": None,
            "dead": None,
            "timeout": None,
            "pass_rate": None,
        },
        "pass_rate": None,
        "searched_paths": searched_paths,
        "invocation": {
            "argv": [],
            "cwd": None,
            "solc": None,
            "remappings": [],
            "certora_version": "unavailable",
        },
        "note": "certoraRun not available - install Certora Prover for verification",
    }


# ---------------------------------------------------------------------------
# Verification_Report text printer + parser round-trip (R20.9)
# ---------------------------------------------------------------------------
#
# ``render_report_text`` renders a Verification_Report back into the certoraRun
# TEXT format that ``_parse_text_rules`` consumes, and ``parse_report_text`` is
# the inverse: it feeds a single text blob through the existing parser
# (``_parse_certora_output`` with no structured results and no declared-rule
# hint) and returns a Verification_Report.
#
# The round-trip contract (R20.9): FOR ALL Verification_Reports, printing then
# parsing yields a report whose Verification_Status, rule-name set, per-rule
# verdict, and Vacuous_Rule set equal the original. The printer emits exactly
# the line shapes the parser recognizes, and aligns its wording to the parser's
# regexes for each verdict:
#
#   PASSED / FAILED / TIMEOUT / ERROR / VACUOUS -> ``Rule '<name>' <STATUS>``
#       (the ``rule_pattern`` in ``_parse_text_rules`` recognizes each of these
#        tokens directly, so a ``VACUOUS`` rule round-trips through this line
#        alone).
#   DEAD -> ``Rule '<name>' PASSED`` plus a ``dead code ... rule '<name>'`` line.
#       ``DEAD`` is NOT one of the tokens the ``rule_pattern`` recognizes, so a
#       bare ``Rule '<name>' DEAD`` line would be dropped. Instead the printer
#       emits a status the pattern accepts and then a dead-code line that the
#       ``dead_pattern`` turns back into ``DEAD`` — this is the "align the
#       printer's wording to the parser's regex" case R20.9 calls out.
#
# Because status on parse is recomputed by ``_classify_status`` from the parsed
# rules + warnings (with declared_rules=None), the printed text reproduces the
# original status exactly whenever the original report's status is itself
# consistent with that classification of its own rules + warnings — which is
# what a report produced by this module always is.


def render_report_text(report: dict) -> str:
    """Render a Verification_Report in the certoraRun TEXT format (R20.9).

    Emits, for each rule, a line the text parser recognizes for that rule's
    verdict, plus a ``Warning:`` line per recorded warning (which
    :func:`_parse_warnings` reads back). The output is deterministic: rules are
    emitted in the order they appear in ``report['rules']``.

    Line ordering is kept stable for readability: every ``Rule '<name>' STATUS``
    line is emitted first (a DEAD rule emits a PASSED base line so its name is
    registered), then the ``dead code`` annotation lines are emitted LAST. The
    ``rule_pattern`` in ``_parse_text_rules`` matches the verdict token ON THE
    SAME LINE as the rule name (no re.DOTALL), so a dead-code annotation line —
    which carries no ``Rule '<name>' VERDICT`` shape — cannot be misread as a
    rule verdict. The ``dead_pattern`` then rewrites each annotated rule's
    verdict back to ``DEAD``.

    Warnings are emitted between the rule lines and the dead-code lines; warning
    messages carry no verdict token, so they do not disturb parsing.

    The printer is the inverse of :func:`parse_report_text` over the fields
    R20.9 requires to be preserved: the Verification_Status, the rule-name set,
    each rule's per-rule verdict, and the Vacuous_Rule set.
    """
    rule_lines: list[str] = []
    dead_lines: list[str] = []
    for rule in report.get("rules") or []:
        name = str(rule.get("name", ""))
        status = str(rule.get("status", "PASSED")).upper()
        if status == "DEAD":
            # DEAD is not a token rule_pattern accepts; register the name with a
            # PASSED base line and annotate it as dead code (emitted last).
            rule_lines.append(f"Rule '{name}' PASSED")
            dead_lines.append(f"dead code detected in rule '{name}'")
        else:
            # PASSED / FAILED / TIMEOUT / ERROR / VACUOUS recognized directly.
            rule_lines.append(f"Rule '{name}' {status}")

    lines = list(rule_lines)
    for warning in report.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    lines.extend(dead_lines)
    return "\n".join(lines) + ("\n" if lines else "")


def parse_report_text(text: str) -> dict:
    """Parse a certoraRun text blob into a Verification_Report (R20.9).

    A thin wrapper over :func:`_parse_certora_output`: there is no structured
    results file (``output_dir=None``) and no declared-rule hint, so verdicts
    and warnings are read from the text and the status is re-derived by
    :func:`_classify_status` from those parsed verdicts and warnings. This is
    the inverse of :func:`render_report_text` for the R20.9 round-trip fields.
    """
    return _parse_certora_output(text or "", "", output_dir=None, declared_rules=None)
