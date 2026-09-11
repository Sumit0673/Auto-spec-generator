"""
Pipeline Orchestrator - Runs all 5 stages in sequence.

Every cached load routes through the Artifact_Store (:mod:`spec_pipeline.artifacts`):
:func:`~spec_pipeline.artifacts.load_artifact` reads the ``{provenance, payload}``
envelope, :func:`~spec_pipeline.artifacts.is_stale` decides whether the recorded
artifact still matches the current sources, and a stale-or-absent artifact makes
the orchestrator re-run the producing stage (Requirement 2.2).

This replaces the old ``_load_stage1.._load_stage5`` helpers, whose Stage 1
loader returned an EMPTY table (the core defect) and which read
``project_stage1.json`` while Stage 1 wrote ``{dir.name}_stage1.json``. The base
name now comes from the single :func:`~spec_pipeline.artifacts.artifact_base_name`
function for both reads and writes, so file and directory inputs are consistent
(Requirement 2.5).

For stage N>=2 the Stage 1 table is obtained from the in-process extractor (when
Stage 1 ran this invocation) or from a fresh, non-stale Stage 1 artifact via
:func:`~spec_pipeline.artifacts.deserialize_stage1` - never an empty fallback
(Requirement 4.2). This is the fix for the empty-table defect.

Seams left open on purpose (not implemented here):

* Task 4.2 - zero-contract short-circuit and Outcome_Set exit codes. The
  orchestrator already threads a ``disposition`` per stage and knows the Stage 1
  contract count, so the short-circuit slots in where noted.
* Task 4.3 - ``--no-cache`` / ``--require-cache`` cache-control flags. Every
  cached load funnels through :func:`_load_stage_artifact`, the single seam those
  flags will consult.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import artifacts
from .artifacts import (
    LoadResult,
    Provenance,
    artifact_base_name,
    deserialize_stage1,
    is_stale,
    load_artifact,
    serialize_stage1,
    source_fingerprint,
)
from .stage1_extract import CompileError, extract_first_party, Stage1Table
from .stage2_invariants import mine_invariants
from .stage3_rules import write_rules
from .stage3_iterative import write_rules_iterative
from .stage4_critic import criticize, apply_findings
from .stage5_verify import verify_with_prover
from .outcomes import exit_code_for

# Single source of truth for the running Spec_Pipeline version. Recorded into
# every artifact's provenance and compared by ``is_stale`` (Requirement 3.4).
PIPELINE_VERSION = "0.1.0"


def _default_output_dir() -> Path:
    """The default pipeline output directory: ``spec_pipeline/Output``.

    Resolved relative to this package's location so it is stable regardless of
    the process cwd. This replaces the old ``Path.cwd() / "spec_pipeline_output"``
    default, which depended on where the process was launched.
    """
    return Path(__file__).resolve().parent / "Output"

# Per-stage disposition values recorded in the Run_Manifest (Requirement 4.6).
DISP_EXECUTED = "executed"
DISP_LOADED_CACHED = "loaded_cached"
DISP_REPORTED_OUTCOME = "reported_outcome"
# A requested stage that never ran because an earlier outcome stopped the run.
DISP_NOT_RUN = "not_run"

# Outcome_Set member reported when the Stage 1 table available to a stage
# numbered >=2 declares zero contracts (Requirement 4.3). The Outcome->exit-code
# table in the design (Data Models) maps this outcome to exit code 4.
OUTCOME_NO_FIRST_PARTY_CONTRACTS = "no_first_party_contracts"
EXIT_NO_FIRST_PARTY_CONTRACTS = 4

# Outcome_Set member reported when slither/solc cannot compile the analyzed
# project (Requirement 19.6). The extractor signals this by raising
# ``stage1_extract.CompileError``; the orchestrator converts it to a
# ``PipelineOutcome`` carrying this outcome and exit code 7 (the design's
# Outcome->exit-code table), writes no Stage 1 artifact, and stops before Stage 2.
OUTCOME_COMPILE_FAILED = "compile_failed"
EXIT_COMPILE_FAILED = 7

# The ``--require-cache`` violation (Requirement 3.8). Per the design, exit code
# 3 is reserved for this case and is deliberately NOT one of the Outcome_Set
# exit codes in ``outcomes.OUTCOME_EXIT_CODES``. It is represented as a distinct
# terminal: a ``PipelineOutcome`` carrying this marker outcome and an explicit
# ``exit_code=3`` that the CLI honors directly, keeping exit 3 out of the
# Outcome_Set table.
OUTCOME_REQUIRE_CACHE_MISS = "require_cache_miss"
EXIT_REQUIRE_CACHE_MISS = 3


class PipelineOutcome(Exception):
    """Raised to stop the run early with a classified Outcome_Set member.

    Carries the ``outcome`` string and the ``exit_code`` the CLI maps it to
    (task 7.1 consumes ``results["outcome"]`` / ``results["exit_code"]`` and this
    exception at the CLI boundary). ``results`` holds the partially populated
    results dict at the point the run stopped, so the CLI can still emit the
    Run_Manifest with per-stage dispositions.
    """

    def __init__(self, outcome: str, exit_code: int, message: str,
                 results: Optional[dict] = None):
        super().__init__(message)
        self.outcome = outcome
        self.exit_code = exit_code
        self.results = results


# ---------------------------------------------------------------------------
# Pure decision + recording helpers (slither-free, unit-testable in isolation)
# ---------------------------------------------------------------------------


def _zero_contract_outcome(
    contract_count: int,
    requested_stages: list[int],
) -> Optional[tuple[str, int]]:
    """Return the short-circuit outcome when there is nothing for stage >=2 to do.

    The Stage 1 table available to a stage numbered >=2 declaring zero contracts
    means every LLM-driven stage would analyze an empty table, so the run stops
    before any LLM call and reports ``no_first_party_contracts`` (Requirement
    4.3). The design's Outcome->exit-code table maps that outcome to exit 4.

    Pure and slither-free by construction: it takes only the contract count and
    the requested stage list, so a unit test exercises the decision without
    importing the extractor chain.

    Args:
        contract_count: ``len(stage1_table.contracts)``.
        requested_stages: The stages the caller asked to run.

    Returns:
        ``(outcome, exit_code)`` when ``contract_count == 0`` AND at least one
        requested stage is numbered >=2; otherwise ``None`` (no short-circuit).
    """
    if contract_count == 0 and any(stage >= 2 for stage in requested_stages):
        return OUTCOME_NO_FIRST_PARTY_CONTRACTS, EXIT_NO_FIRST_PARTY_CONTRACTS
    return None


def _compile_failed_outcome(
    error: CompileError,
    sol_path: Path,
    results: Optional[dict] = None,
) -> PipelineOutcome:
    """Convert a :class:`CompileError` into a ``compile_failed`` PipelineOutcome.

    Builds the exit-7 ``compile_failed`` terminal (Requirement 19.6, design
    Outcome->exit-code table). The message carries the compiler diagnostics, the
    attempted solc version, and the applied remappings so an operator can act;
    no Stage 1 artifact is written and no later stage runs because the raise
    happens at the extraction seam before any artifact write.

    Pure and slither-free: it only formats the already-captured error fields, so
    it is unit-testable by constructing a ``CompileError`` directly.

    Args:
        error: The :class:`CompileError` the extractor raised.
        sol_path: The analyzed source path (for the message).
        results: Optional partial results dict to attach for the CLI.

    Returns:
        A :class:`PipelineOutcome` with ``outcome == "compile_failed"`` and
        ``exit_code == 7``.
    """
    remaps = ", ".join(error.remappings) if error.remappings else "(none)"
    message = (
        f"{OUTCOME_COMPILE_FAILED}: slither could not compile {sol_path} "
        f"(attempted solc={error.solc_version}; remappings={remaps}): "
        f"{error.diagnostics}"
    )
    return PipelineOutcome(
        OUTCOME_COMPILE_FAILED,
        EXIT_COMPILE_FAILED,
        message,
        results=results,
    )


def _artifact_sha256(output_dir: Path, base: str, stage: int) -> Optional[str]:
    """Return the SHA-256 of a written stage envelope's bytes, or None if absent.

    Computed over the exact bytes on disk so the recorded digest matches the
    canonical artifact the Artifact_Store wrote (Requirement 21.8).
    """
    path = Path(output_dir) / f"{base}_stage{stage}.json"
    if not path.exists():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _record_stage_manifest(
    manifest: dict,
    stage: int,
    disposition: str,
    *,
    duration_s: Optional[float] = None,
    artifact_sha256: Optional[str] = None,
    outcome: Optional[str] = None,
) -> None:
    """Record exactly one disposition entry for a requested stage (R4.6, R21.8).

    Writes a per-stage ``{disposition, duration_s, artifact_sha256}`` entry into
    ``manifest["stage_details"]`` and mirrors the bare disposition into
    ``manifest["stage_dispositions"]`` (kept for the task 4.1 shape). For a
    not-run stage, ``outcome`` names the Outcome_Set member that stopped it.
    """
    key = f"stage{stage}"
    manifest["stage_dispositions"][key] = disposition
    entry: dict[str, Any] = {
        "disposition": disposition,
        "duration_s": duration_s,
        "artifact_sha256": artifact_sha256,
    }
    if outcome is not None:
        entry["outcome"] = outcome
    manifest.setdefault("stage_details", {})[key] = entry


def _record_not_run_stages(
    manifest: dict,
    requested_stages: list[int],
    outcome: str,
) -> None:
    """Record every requested stage that has no disposition yet as not-run (R4.6).

    Each remaining requested stage numbered >=2 gets a ``not_run`` disposition
    naming the Outcome_Set member that stopped it. Stages already recorded
    (e.g. stage 1, which executed or loaded) are left untouched.
    """
    for stage in requested_stages:
        key = f"stage{stage}"
        if key in manifest["stage_dispositions"]:
            continue
        _record_stage_manifest(
            manifest, stage, DISP_NOT_RUN, outcome=outcome
        )


# ---------------------------------------------------------------------------
# Fingerprint / provenance helpers
# ---------------------------------------------------------------------------


def _first_party_sol_files(sol_path: Path) -> list[Path]:
    """Return the ``.sol`` files fingerprinted for staleness detection.

    A file input contributes only itself; a directory input contributes every
    ``.sol`` file beneath it. Kept deliberately simple: the Dependency_Resolver
    filtering that excludes dependency roots lives in Stage 1, and the fingerprint
    only needs to change when a first-party source changes.
    """
    if sol_path.is_file():
        return [sol_path]
    return sorted(sol_path.rglob("*.sol"))


def _current_fingerprint(sol_path: Path) -> str:
    """Compute the Source_Fingerprint of the analyzed sources (Requirement 3.2)."""
    return source_fingerprint(sol_path, _first_party_sol_files(sol_path))


def _make_provenance(
    stage: int,
    sol_path: Path,
    fingerprint: str,
    consumed: Optional[list[tuple[int, str]]] = None,
) -> Provenance:
    """Build a Provenance record for a stage that ran this invocation (R3.1)."""
    return Provenance(
        pipeline_version=PIPELINE_VERSION,
        stage=stage,
        completed_utc=datetime.now(timezone.utc).isoformat(),
        source_path=str(sol_path),
        source_fingerprint=fingerprint,
        consumed=consumed or [],
    )


# ---------------------------------------------------------------------------
# Generic cached-load seam (Artifact_Store) - Requirements 2.2, 3.3-3.6
# ---------------------------------------------------------------------------


def _load_stage_artifact(
    output_dir: Path,
    base: str,
    stage: int,
    fingerprint: str,
    no_cache: bool = False,
) -> tuple[Optional[LoadResult], Optional[artifacts.Staleness]]:
    """Load a stage artifact and classify its freshness.

    Returns ``(load_result, staleness)`` where:

    * ``load_result`` is ``None`` when no artifact file exists on disk;
    * ``staleness`` is ``None`` when the artifact is present AND fresh;
    * a non-``None`` ``staleness`` names the single reason the artifact is stale.

    This is the ONE seam every cached load funnels through, so the ``--no-cache``
    (force miss) and ``--require-cache`` (fail on miss/stale) flags slot in here
    without touching the stage bodies.

    When *no_cache* is set the load is short-circuited to a "miss" -
    ``(None, None)`` - WITHOUT reading the on-disk artifact, so every requested
    stage runs from its inputs and overwrites its artifact and artifacts already
    on disk are left unread (Requirement 3.7).
    """
    # ``--no-cache``: force a miss so the caller re-runs the stage from inputs;
    # the on-disk artifact is never read (Requirement 3.7).
    if no_cache:
        return None, None
    result = load_artifact(output_dir, base, stage)
    if not result.present:
        return None, None
    staleness = is_stale(
        result.provenance,
        current_fingerprint=fingerprint,
        current_version=PIPELINE_VERSION,
        # Stage-to-stage consumed-digest wiring is a later refinement; passing an
        # empty map means the ``consumed`` reason only fires when a recorded input
        # stage is missing, never on a false digest mismatch.
        disk_inputs={},
    )
    return result, staleness


def _report_skip(base: str, stage: int, output_dir: Path, result: LoadResult) -> None:
    """Report a skipped stage: artifact path + recorded completion time (R4.5)."""
    path = output_dir / f"{base}_stage{stage}.json"
    completed = result.provenance.completed_utc if result.provenance else "unknown"
    print(
        f"  Stage {stage}: using fresh cached artifact {path} "
        f"(completed {completed})"
    )


def _report_stale(
    base: str, stage: int, output_dir: Path, staleness: artifacts.Staleness,
    fingerprint: str,
) -> None:
    """Report a stale artifact: path, recorded vs current, re-run stage (R3.6)."""
    path = output_dir / f"{base}_stage{stage}.json"
    print(
        f"  Stage {stage}: cached artifact {path} is stale "
        f"(reason={staleness.reason}, recorded={staleness.recorded!r}, "
        f"current={staleness.current!r}); re-running stage {stage}"
    )


def _require_cache_check(
    output_dir: Path,
    base: str,
    stage: int,
    result: Optional[LoadResult],
    staleness: Optional[artifacts.Staleness],
) -> None:
    """Enforce ``--require-cache`` for a requested stage's prerequisite (R3.8).

    Given the ``(result, staleness)`` returned by :func:`_load_stage_artifact`
    for a prerequisite artifact, raise the dedicated exit-3
    :class:`PipelineOutcome` when that artifact is absent or stale. The message
    names the artifact and states whether it was absent or stale together with
    the staleness reason (Requirements 3.6, 3.8). No stage runs and no LLM call
    is issued because the raise happens before the stage body.

    A present, fresh artifact (``result is not None and staleness is None``)
    passes silently and the run proceeds.
    """
    path = output_dir / f"{base}_stage{stage}.json"

    if result is None:
        raise PipelineOutcome(
            OUTCOME_REQUIRE_CACHE_MISS,
            EXIT_REQUIRE_CACHE_MISS,
            f"--require-cache: prerequisite artifact {path} for stage {stage} "
            f"is absent",
        )

    if staleness is not None:
        raise PipelineOutcome(
            OUTCOME_REQUIRE_CACHE_MISS,
            EXIT_REQUIRE_CACHE_MISS,
            f"--require-cache: prerequisite artifact {path} for stage {stage} "
            f"is stale (reason={staleness.reason}, "
            f"recorded={staleness.recorded!r}, current={staleness.current!r})",
        )


# ---------------------------------------------------------------------------
# Stage 1: load-or-run helper (pure w.r.t. the extractor; slither-free testable)
# ---------------------------------------------------------------------------


def _load_or_run_stage1(
    output_dir: Path,
    base: str,
    sol_path: Path,
    extractor: Callable[[Path, Path], Stage1Table],
    fingerprint: str,
    no_cache: bool = False,
    require_cache: bool = False,
) -> tuple[Stage1Table, str]:
    """Return a Stage 1 table plus its disposition, loading cache or extracting.

    The Stage 1 table is obtained from a fresh, non-stale artifact when one is on
    disk, or from *extractor* otherwise - NEVER an empty fallback (Requirement
    4.2). This is the fix for the empty-table defect.

    *extractor* is injected as ``(sol_path, output_dir) -> Stage1Table`` so this
    helper is exercisable without slither: a test passes a fake extractor and a
    pre-written Stage 1 artifact and asserts the load path deserializes the real
    (non-empty) table, and that a stale/absent artifact triggers re-extraction.

    When *no_cache* is set the on-disk artifact is left unread and the extractor
    always runs (Requirement 3.7). When *require_cache* is set an absent or stale
    Stage 1 artifact raises the exit-3 :class:`PipelineOutcome` before the
    extractor runs (Requirement 3.8); ``--require-cache`` therefore takes
    precedence and is never combined with a forced miss here (Stage 1 has no
    prerequisite, but requesting Stage 1 under ``--require-cache`` still demands a
    fresh Stage 1 artifact when the caller obtains it from cache).

    Returns ``(table, disposition)`` where disposition is ``loaded_cached`` or
    ``executed``.
    """
    result, staleness = _load_stage_artifact(
        output_dir, base, 1, fingerprint, no_cache=no_cache
    )

    # ``--require-cache``: a requested stage's prerequisite that is absent or
    # stale exits 3 naming the artifact, running no stage (Requirement 3.8).
    if require_cache:
        _require_cache_check(output_dir, base, 1, result, staleness)

    if result is not None and staleness is None:
        # Present and fresh: reconstruct the full typed table from the envelope.
        artifact_path = output_dir / f"{base}_stage1.json"
        table = deserialize_stage1(result.payload, context=str(artifact_path))
        _report_skip(base, 1, output_dir, result)
        return table, DISP_LOADED_CACHED

    if result is not None and staleness is not None:
        _report_stale(base, 1, output_dir, staleness, fingerprint)

    # Absent or stale: re-run the producing stage from its inputs (Requirement 2.2).
    table = extractor(sol_path, output_dir)
    _write_stage1_envelope(output_dir, base, sol_path, table, fingerprint)
    return table, DISP_EXECUTED


def _write_stage1_envelope(
    output_dir: Path,
    base: str,
    sol_path: Path,
    table: Stage1Table,
    fingerprint: str,
) -> None:
    """Write the Stage 1 Artifact_Store envelope so loads round-trip (R2.1).

    ``extract_first_party`` still writes its bare ``{base}_stage1.txt`` sidecar
    for human reading; here we additionally write the canonical
    ``{base}_stage1.json`` envelope (serialized via :func:`serialize_stage1` with
    a stage-1 Provenance) which is the artifact the loader reads back.
    """
    prov = _make_provenance(1, sol_path, fingerprint, consumed=[])
    artifacts.write_artifact(
        output_dir, base, 1, serialize_stage1(table), prov
    )


# ---------------------------------------------------------------------------
# Stages 2-5: envelope writers (dual-write alongside each stage's own sidecar)
# ---------------------------------------------------------------------------


def _write_stage_envelope(
    output_dir: Path,
    base: str,
    stage: int,
    sol_path: Path,
    payload: dict,
    fingerprint: str,
) -> None:
    """Write an Artifact_Store envelope for stages 2-5 so loads round-trip.

    Each stage keeps its own bare ``.cvl``/``.json`` sidecar (unchanged) for now;
    this adds the canonical envelope the orchestrator reads back on a cached run.
    """
    prov = _make_provenance(stage, sol_path, fingerprint, consumed=[])
    artifacts.write_artifact(output_dir, base, stage, payload, prov)


def _load_or_run_generic(
    output_dir: Path,
    base: str,
    stage: int,
    fingerprint: str,
    run: Callable[[], dict],
    no_cache: bool = False,
    require_cache: bool = False,
) -> tuple[dict, str]:
    """Load a fresh stage 2-5 envelope's payload, else run *run* and persist it.

    Returns ``(payload, disposition)``. ``run`` must return the payload dict to
    persist; on a cache hit the recorded payload is returned untouched.

    When *no_cache* is set the on-disk artifact is left unread and *run* always
    executes (Requirement 3.7). When *require_cache* is set an absent or stale
    artifact raises the exit-3 :class:`PipelineOutcome` before *run* is called,
    so no stage runs and no LLM call is issued (Requirement 3.8).
    """
    result, staleness = _load_stage_artifact(
        output_dir, base, stage, fingerprint, no_cache=no_cache
    )
    if require_cache:
        _require_cache_check(output_dir, base, stage, result, staleness)
    if result is not None and staleness is None:
        _report_skip(base, stage, output_dir, result)
        return result.payload, DISP_LOADED_CACHED
    if result is not None and staleness is not None:
        _report_stale(base, stage, output_dir, staleness, fingerprint)
    payload = run()
    return payload, DISP_EXECUTED


def run_pipeline(
    sol_path: str | Path,
    output_dir: str | Path | None = None,
    stages: list[int] | None = None,
    certora_args: list[str] | None = None,
    iterative_stage3: bool = False,
    max_iterations: int = 3,
    no_cache: bool = False,
    require_cache: bool = False,
    typecheck_only: bool = False,
) -> dict:
    """
    Run the full 5-stage spec generation pipeline.

    Args:
        sol_path: Path to .sol file or directory
        output_dir: Output directory (default: spec_pipeline/Output)
        stages: Which stages to run [1,2,3,4,5] (default: all)
        certora_args: Extra args for certoraRun in Stage 5
        iterative_stage3: If True, run Stage 3 with certoraRun feedback loop
        max_iterations: Max iterations for iterative Stage 3 (default: 3)
        no_cache: If True, run every requested stage from its inputs and leave
            on-disk artifacts unread, overwriting each stage's artifact
            (Requirement 3.7). Behavior-preserving default ``False`` (R1.6).
        require_cache: If True, a requested stage whose prerequisite artifact is
            absent or stale stops the run with exit code 3, naming that artifact
            and stating absent vs stale plus the staleness reason, running no
            stage and issuing no LLM call (Requirement 3.8). Behavior-preserving
            default ``False`` (R1.6).
        typecheck_only: If True, use the KEYLESS local CVL typecheck as the
            feedback signal and STOP at a clean typecheck; the cloud rule-proof
            is NEVER attempted even when CERTORAKEY is set. Stage 3 iterates
            forcing the keyless path (``write_rules_iterative(force_keyless=True)``)
            and Stage 5 runs only ``local_typecheck`` via
            ``verify_with_prover(typecheck_only=True)``, mapping the local result
            (typecheck_passed / typecheck_failed / setup_failed / tool_unavailable)
            to the run outcome. Implies the iterative Stage 3 loop. Default
            ``False`` (behavior-preserving).

    Returns:
        dict: Summary of all stages

    Raises:
        PipelineOutcome: With ``exit_code == 3`` when *require_cache* is set and a
            requested stage's prerequisite artifact is absent or stale.
    """
    sol_path = Path(sol_path).resolve()

    # ``--typecheck-only`` implies the iterative Stage 3 feedback loop so
    # failures are actually fixed against the keyless typecheck signal.
    if typecheck_only:
        iterative_stage3 = True

    if output_dir is None:
        output_dir = _default_output_dir()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stages = stages or [1, 2, 3, 4, 5]

    # Single base name for every read and write (Requirement 2.5).
    base = artifact_base_name(sol_path)
    fingerprint = _current_fingerprint(sol_path)

    results = {
        "source_path": str(sol_path),
        "output_dir": str(output_dir),
        "artifact_base": base,
        "source_fingerprint": fingerprint,
        "stages": {},
        # Terminal Outcome_Set member + its mapped exit code. ``None`` while the
        # run proceeds normally; the CLI (task 7.1) maps a set ``outcome`` to its
        # exit code via the design's Outcome->exit-code table.
        "outcome": None,
        "exit_code": None,
        # Per requested stage: a bare disposition (task 4.1 shape) plus a richer
        # ``{disposition, duration_s, artifact_sha256[, outcome]}`` detail entry
        # (R4.6, R21.8). Full Run_Manifest persistence is task 21.x.
        "run_manifest": {"stage_dispositions": {}, "stage_details": {}},
    }
    manifest = results["run_manifest"]

    # Stage 1: First-Party Extraction
    if 1 in stages:
        print("\n" + "="*60)
        print("STAGE 1: First-Party Extraction")
        print("="*60)
        _t0 = time.monotonic()
        try:
            stage1_table = extract_first_party(sol_path, output_dir)
        except CompileError as exc:
            # Compile failure: report ``compile_failed`` (exit 7), write no Stage
            # 1 artifact, stop before Stage 2 (Requirement 19.6).
            raise _compile_failed_outcome(exc, sol_path, results)
        _write_stage1_envelope(output_dir, base, sol_path, stage1_table, fingerprint)
        results["stages"]["stage1"] = {
            "contracts": len(stage1_table.contracts),
            "contract_names": list(stage1_table.contracts.keys()),
        }
        _record_stage_manifest(
            manifest, 1, DISP_EXECUTED,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 1),
        )
    else:
        # Obtain Stage 1 from a fresh artifact, or re-extract - never empty (R4.2).
        _t0 = time.monotonic()
        try:
            stage1_table, disp = _load_or_run_stage1(
                output_dir, base, sol_path, extract_first_party, fingerprint,
                no_cache=no_cache, require_cache=require_cache,
            )
        except CompileError as exc:
            raise _compile_failed_outcome(exc, sol_path, results)
        _record_stage_manifest(
            manifest, 1, disp,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 1),
        )
        results["stages"]["stage1"] = {
            "contracts": len(stage1_table.contracts),
            "contract_names": list(stage1_table.contracts.keys()),
        }

    # Zero-contract short-circuit (task 4.2, Requirement 4.3): if the Stage 1
    # table available to a stage numbered >=2 declares zero contracts, stop
    # before any LLM call, report ``no_first_party_contracts`` (exit 4), and
    # record the remaining requested stages as not-run with that outcome.
    _sc = _zero_contract_outcome(len(stage1_table.contracts), stages)
    if _sc is not None:
        outcome, exit_code = _sc
        results["outcome"] = outcome
        results["exit_code"] = exit_code
        _record_not_run_stages(manifest, stages, outcome)
        print(
            f"\nOutcome: {outcome} (exit {exit_code}) - "
            f"zero first-party contracts in {sol_path}; "
            f"stopping before any LLM stage."
        )
        _write_summary(results, output_dir, base)
        return results

    # Stage 2: Invariant Mining
    if 2 in stages:
        print("\n" + "="*60)
        print("STAGE 2: Invariant Mining")
        print("="*60)
        _t0 = time.monotonic()
        invariants = mine_invariants(stage1_table, sol_path, output_dir)
        _write_stage_envelope(
            output_dir, base, 2, sol_path, invariants, fingerprint
        )
        results["stages"]["stage2"] = {
            "contracts_with_invariants": len(invariants),
            "total_variables": sum(len(v) for v in invariants.values()),
        }
        _record_stage_manifest(
            manifest, 2, DISP_EXECUTED,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 2),
        )
    else:
        _t0 = time.monotonic()
        invariants, disp = _load_or_run_generic(
            output_dir, base, 2, fingerprint,
            run=lambda: _run_stage2_and_persist(
                stage1_table, sol_path, output_dir, base, fingerprint
            ),
            no_cache=no_cache, require_cache=require_cache,
        )
        _record_stage_manifest(
            manifest, 2, disp,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 2),
        )

    # Stage 3: Rule Writing (with optional iterative feedback loop)
    if 3 in stages:
        print("\n" + "="*60)
        print("STAGE 3: Per-Contract Rule Writing")
        if iterative_stage3:
            print(" (with certoraRun feedback loop)")
        print("="*60)

        _t0 = time.monotonic()
        if iterative_stage3:
            iterative_result = write_rules_iterative(
                stage1_table, invariants, sol_path, output_dir,
                max_iterations=max_iterations, certora_args=certora_args,
                force_keyless=typecheck_only,
            )
            cvl_spec = iterative_result["final_spec"]
            results["stages"]["stage3"] = {
                "spec_length": len(cvl_spec),
                "rules_count": cvl_spec.count("rule "),
                "invariants_count": cvl_spec.count("invariant "),
                "iterations": len(iterative_result["iterations"]),
                "final_report": iterative_result["final_report"],
            }
        else:
            cvl_spec = write_rules(stage1_table, invariants, sol_path, output_dir)
            results["stages"]["stage3"] = {
                "spec_length": len(cvl_spec),
                "rules_count": cvl_spec.count("rule "),
                "invariants_count": cvl_spec.count("invariant "),
            }
        _write_stage_envelope(
            output_dir, base, 3, sol_path, {"cvl_spec": cvl_spec}, fingerprint
        )
        _record_stage_manifest(
            manifest, 3, DISP_EXECUTED,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 3),
        )
    else:
        _t0 = time.monotonic()
        payload, disp = _load_or_run_generic(
            output_dir, base, 3, fingerprint,
            run=lambda: _run_stage3_and_persist(
                stage1_table, invariants, sol_path, output_dir, base, fingerprint
            ),
            no_cache=no_cache, require_cache=require_cache,
        )
        cvl_spec = payload.get("cvl_spec", "")
        _record_stage_manifest(
            manifest, 3, disp,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 3),
        )

    # Stage 4: Adversarial Critic
    if 4 in stages:
        print("\n" + "="*60)
        print("STAGE 4: Adversarial Critic")
        print("="*60)
        _t0 = time.monotonic()
        findings = criticize(sol_path, cvl_spec, stage1_table, output_dir)
        results["stages"]["stage4"] = {
            "findings_count": len(findings),
            "high_severity": sum(1 for f in findings if f.get("severity") == "high"),
        }

        # Apply findings to produce repaired spec for Stage 5
        if findings:
            cvl_spec = apply_findings(findings, cvl_spec, sol_path, output_dir)
            results["stages"]["stage4"]["spec_repaired"] = True
        _write_stage_envelope(
            output_dir, base, 4, sol_path,
            {"findings": findings, "cvl_spec": cvl_spec}, fingerprint
        )
        _record_stage_manifest(
            manifest, 4, DISP_EXECUTED,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 4),
        )
    else:
        _t0 = time.monotonic()
        payload, disp = _load_or_run_generic(
            output_dir, base, 4, fingerprint,
            run=lambda: _run_stage4_and_persist(
                sol_path, cvl_spec, stage1_table, output_dir, base, fingerprint
            ),
            no_cache=no_cache, require_cache=require_cache,
        )
        findings = payload.get("findings", [])
        # A cached stage 4 may carry a repaired spec; prefer it for stage 5.
        cvl_spec = payload.get("cvl_spec", cvl_spec)
        _record_stage_manifest(
            manifest, 4, disp,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 4),
        )

    # Stage 5: Prover + Vacuity (only if not already done in iterative mode)
    if 5 in stages and not (3 in stages and iterative_stage3):
        print("\n" + "="*60)
        print("STAGE 5: Prover + Vacuity Check")
        print("="*60)
        _t0 = time.monotonic()
        report = verify_with_prover(
            sol_path, cvl_spec, output_dir, certora_args, table=stage1_table,
            run_local_typecheck=True, typecheck_only=typecheck_only,
        )
        _write_stage_envelope(output_dir, base, 5, sol_path, report, fingerprint)
        results["stages"]["stage5"] = report["summary"]
        _record_stage_manifest(
            manifest, 5, DISP_EXECUTED,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 5),
        )
    elif 5 in stages and iterative_stage3:
        # Already verified in the iterative loop; load the final report envelope.
        _t0 = time.monotonic()
        payload, disp = _load_or_run_generic(
            output_dir, base, 5, fingerprint,
            run=lambda: verify_with_prover(
                sol_path, cvl_spec, output_dir, certora_args, table=stage1_table,
                run_local_typecheck=True, typecheck_only=typecheck_only,
            ),
            no_cache=no_cache, require_cache=require_cache,
        )
        report = payload
        results["stages"]["stage5"] = report.get("summary", {})
        _record_stage_manifest(
            manifest, 5, disp,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 5),
        )
    elif 5 in stages:
        _t0 = time.monotonic()
        payload, disp = _load_or_run_generic(
            output_dir, base, 5, fingerprint,
            run=lambda: verify_with_prover(
                sol_path, cvl_spec, output_dir, certora_args, table=stage1_table,
                run_local_typecheck=True, typecheck_only=typecheck_only,
            ),
            no_cache=no_cache, require_cache=require_cache,
        )
        report = payload
        results["stages"]["stage5"] = report.get("summary", {})
        _record_stage_manifest(
            manifest, 5, disp,
            duration_s=time.monotonic() - _t0,
            artifact_sha256=_artifact_sha256(output_dir, base, 5),
        )

    # In ``--typecheck-only`` mode the run outcome is the local typecheck result
    # recorded in the Stage 5 report (typecheck_passed / typecheck_failed /
    # setup_failed / tool_unavailable). Surface it as the terminal Outcome_Set
    # member + exit code so a clean typecheck-only run exits 0 instead of the
    # misleading generic ``error`` (12). No cloud proof was ever run.
    if typecheck_only and 5 in stages:
        _tc_status = report.get("status") if isinstance(report, dict) else None
        if _tc_status:
            results["outcome"] = _tc_status
            results["exit_code"] = exit_code_for(_tc_status)

    # Final summary
    print("\n" + "="*60)
    print("PIPELINE COMPLETE")
    print("="*60)
    for stage_num in stages:
        key = f"stage{stage_num}"
        if key in results["stages"]:
            print(f"  Stage {stage_num}: {results['stages'][key]}")

    # Save full results
    _write_summary(results, output_dir, base)

    return results


def _write_summary(results: dict, output_dir: Path, base: str) -> None:
    """Persist the run results (including the Run_Manifest) as JSON."""
    summary_file = Path(output_dir) / f"{base}_pipeline_summary.json"
    with open(summary_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull summary saved to {summary_file}")


# ---------------------------------------------------------------------------
# Stage run-and-persist helpers (run the stage, then dual-write its envelope)
# ---------------------------------------------------------------------------


def _run_stage2_and_persist(
    table: Stage1Table, sol_path: Path, output_dir: Path, base: str, fingerprint: str
) -> dict:
    invariants = mine_invariants(table, sol_path, output_dir)
    _write_stage_envelope(output_dir, base, 2, sol_path, invariants, fingerprint)
    return invariants


def _run_stage3_and_persist(
    table: Stage1Table, invariants: dict, sol_path: Path, output_dir: Path,
    base: str, fingerprint: str,
) -> dict:
    cvl_spec = write_rules(table, invariants, sol_path, output_dir)
    payload = {"cvl_spec": cvl_spec}
    _write_stage_envelope(output_dir, base, 3, sol_path, payload, fingerprint)
    return payload


def _run_stage4_and_persist(
    sol_path: Path, cvl_spec: str, table: Stage1Table, output_dir: Path,
    base: str, fingerprint: str,
) -> dict:
    findings = criticize(sol_path, cvl_spec, table, output_dir)
    repaired = cvl_spec
    if findings:
        repaired = apply_findings(findings, cvl_spec, sol_path, output_dir)
    payload = {"findings": findings, "cvl_spec": repaired}
    _write_stage_envelope(output_dir, base, 4, sol_path, payload, fingerprint)
    return payload


def run_single_stage(
    stage: int,
    sol_path: str | Path,
    output_dir: str | Path | None = None,
    iterative_stage3: bool = False,
    max_iterations: int = 3,
    no_cache: bool = False,
    require_cache: bool = False,
    typecheck_only: bool = False,
    **kwargs,
) -> Any:
    """Run a single stage by number, loading cached outputs from prior stages.

    Args:
        stage: The stage number to run.
        sol_path: Path to .sol file or directory.
        output_dir: Output directory (default: spec_pipeline/Output).
        iterative_stage3: Run Stage 3 with the certoraRun feedback loop.
        max_iterations: Max iterations for iterative Stage 3.
        no_cache: If True, the requested stage runs from its inputs and every
            prerequisite load leaves the on-disk artifact unread, re-running it
            (Requirement 3.7). Behavior-preserving default ``False`` (R1.6).
        require_cache: If True, a prerequisite artifact that is absent or stale
            stops the run with exit code 3, naming that artifact and running no
            stage and issuing no LLM call (Requirement 3.8). Behavior-preserving
            default ``False`` (R1.6).
        typecheck_only: If True, use the KEYLESS local CVL typecheck as the
            feedback signal and STOP at a clean typecheck; the cloud rule-proof
            is NEVER attempted even when CERTORAKEY is set. Stage 3 forces the
            keyless iterative loop (``force_keyless=True``) and Stage 5 runs only
            ``local_typecheck`` via ``verify_with_prover(typecheck_only=True)``.
            Implies the iterative Stage 3 loop. Default ``False``.

    Raises:
        PipelineOutcome: With ``exit_code == 3`` when *require_cache* is set and a
            prerequisite artifact is absent or stale.
    """
    # ``--typecheck-only`` implies the iterative Stage 3 feedback loop.
    if typecheck_only:
        iterative_stage3 = True
    sol_path = Path(sol_path).resolve()

    if output_dir is None:
        output_dir = _default_output_dir()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    base = artifact_base_name(sol_path)
    fingerprint = _current_fingerprint(sol_path)

    if stage == 1:
        try:
            table = extract_first_party(sol_path, output_dir)
        except CompileError as exc:
            raise _compile_failed_outcome(exc, sol_path)
        _write_stage1_envelope(output_dir, base, sol_path, table, fingerprint)
        return table

    # Obtain Stage 1 from a fresh artifact, or re-extract - never empty (R4.2).
    # Under ``--require-cache`` an absent/stale Stage 1 prerequisite exits 3
    # before any stage runs; under ``--no-cache`` it is re-extracted (R3.7, R3.8).
    try:
        table, _ = _load_or_run_stage1(
            output_dir, base, sol_path, extract_first_party, fingerprint,
            no_cache=no_cache, require_cache=require_cache,
        )
    except CompileError as exc:
        raise _compile_failed_outcome(exc, sol_path)

    # Zero-contract short-circuit (task 4.2, Requirement 4.3): a single stage
    # numbered >=2 against an empty Stage 1 table must stop before any LLM call
    # and report ``no_first_party_contracts`` (exit 4). Raise the dedicated
    # PipelineOutcome so the CLI (task 7.1) maps it to exit 4.
    _sc = _zero_contract_outcome(len(table.contracts), [stage])
    if _sc is not None:
        outcome, exit_code = _sc
        raise PipelineOutcome(
            outcome,
            exit_code,
            f"{outcome}: zero first-party contracts in {sol_path}; "
            f"stage {stage} not run.",
        )

    if stage == 2:
        invariants = mine_invariants(table, sol_path, output_dir)
        _write_stage_envelope(output_dir, base, 2, sol_path, invariants, fingerprint)
        return invariants

    # Load Stage 2 (run only if cached artifact absent or stale).
    invariants, _ = _load_or_run_generic(
        output_dir, base, 2, fingerprint,
        run=lambda: _run_stage2_and_persist(
            table, sol_path, output_dir, base, fingerprint
        ),
        no_cache=no_cache, require_cache=require_cache,
    )

    if stage == 3:
        if iterative_stage3:
            iterative_result = write_rules_iterative(
                table, invariants, sol_path, output_dir,
                max_iterations=max_iterations,
                force_keyless=typecheck_only,
            )
            cvl_spec = iterative_result["final_spec"]
            _write_stage_envelope(
                output_dir, base, 3, sol_path, {"cvl_spec": cvl_spec}, fingerprint
            )
            return {
                "spec_length": len(cvl_spec),
                "rules_count": cvl_spec.count("rule "),
                "invariants_count": cvl_spec.count("invariant "),
                "iterations": len(iterative_result["iterations"]),
                "final_report": iterative_result["final_report"],
            }
        else:
            cvl_spec = write_rules(table, invariants, sol_path, output_dir)
            _write_stage_envelope(
                output_dir, base, 3, sol_path, {"cvl_spec": cvl_spec}, fingerprint
            )
            return {
                "spec_length": len(cvl_spec),
                "rules_count": cvl_spec.count("rule "),
                "invariants_count": cvl_spec.count("invariant "),
            }

    # Load Stage 3.
    stage3_payload, _ = _load_or_run_generic(
        output_dir, base, 3, fingerprint,
        run=lambda: _run_stage3_and_persist(
            table, invariants, sol_path, output_dir, base, fingerprint
        ),
        no_cache=no_cache, require_cache=require_cache,
    )
    cvl_spec = stage3_payload.get("cvl_spec", "")

    if stage == 4:
        return criticize(sol_path, cvl_spec, table, output_dir)

    if stage == 5:
        return verify_with_prover(
            sol_path, cvl_spec, output_dir, kwargs.get("certora_args"), table=table,
            run_local_typecheck=True, typecheck_only=typecheck_only,
        )

    raise ValueError(f"Unknown stage: {stage}")
