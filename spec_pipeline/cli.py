"""
CLI Entry Point for 5-Stage Spec Generation Pipeline

Usage:
  python -m spec_pipeline <path> [--stages 1,2,3,4,5] [--output-dir ./out]

Terminal-state handling (task 7.1, Requirements 20.6, 20.10, 21.2)
------------------------------------------------------------------
Every path out of ``main`` funnels through the single Outcome_Set -> exit-code
table in :mod:`spec_pipeline.outcomes`. The process exits ``0`` ONLY for
``verified``, ``verified_with_warnings``, and ``skipped_missing_tool``; every
other classified outcome maps to its distinct documented nonzero code, and a
truly unexpected exception maps to ``error`` (exit 12).

The outcome is read, in priority order, from:

1. a raised :class:`~spec_pipeline.pipeline.PipelineOutcome` signal (task 4.2's
   early short-circuits, e.g. ``no_first_party_contracts``),
2. ``results.get("outcome")`` set by the orchestrator, then
3. the Verification_Status recorded in the Stage 5 report of ``results``.

Secrets are redacted at this sink (Requirement 21.2): before anything is printed
to stdout or stderr, the value of any environment variable whose name ends with
``_API_KEY``, ``_TOKEN``, or ``_SECRET`` is replaced with ``REDACTED`` by
:func:`spec_pipeline.outcomes.redact_secrets`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add parent for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from spec_pipeline.outcomes import Outcome, exit_code_for, redact_secrets
from spec_pipeline.pipeline import PipelineOutcome, run_pipeline, run_single_stage


def _emit(message: str, *, err: bool = False) -> None:
    """Print *message* with secrets redacted at the sink (Requirement 21.2)."""
    print(redact_secrets(message), file=sys.stderr if err else sys.stdout)


def _outcome_from_results(results) -> str | None:
    """Extract the terminal Outcome_Set member from a pipeline ``results`` dict.

    Reads ``results["outcome"]`` first (set by the orchestrator's short-circuits,
    task 4.2), then falls back to the Verification_Status recorded in the Stage 5
    report. Returns ``None`` when ``results`` carries neither, leaving the caller
    to decide (a stage run that reached no terminal verification state).
    """
    if not isinstance(results, dict):
        return None

    outcome = results.get("outcome")
    if outcome:
        return outcome

    # Fall back to the Stage 5 Verification_Status when present. The full report
    # is stored under ``stage5_report`` when the orchestrator threads it; the
    # ``stages.stage5`` summary is a secondary source for the status.
    stage5 = results.get("stage5_report")
    if isinstance(stage5, dict) and stage5.get("status"):
        return stage5["status"]

    stage5_summary = results.get("stages", {}).get("stage5")
    if isinstance(stage5_summary, dict) and stage5_summary.get("status"):
        return stage5_summary["status"]

    return None


def _finish(outcome: str | None, explicit_code: int | None = None) -> int:
    """Map *outcome* to its exit code and report it (Requirements 20.6, 20.10).

    ``verified``/``verified_with_warnings``/``skipped_missing_tool`` -> 0; every
    other recognized outcome -> its distinct nonzero code; an unknown/absent
    outcome -> ``error`` (12).

    When *explicit_code* is given it is honored directly and the shared
    Outcome_Set table is NOT consulted. This is how a raised
    :class:`~spec_pipeline.pipeline.PipelineOutcome` that carries its own
    ``exit_code`` (the ``--require-cache`` violation, exit 3, Requirement 3.8) is
    reported: exit 3 is deliberately kept OUT of ``outcomes.OUTCOME_EXIT_CODES``,
    so it must come straight from the raised signal rather than a table lookup.
    """
    code = explicit_code if explicit_code is not None else exit_code_for(outcome)
    label = outcome if outcome is not None else Outcome.ERROR.value
    _emit(f"Outcome: {label} (exit {code})")
    return code


def main():
    parser = argparse.ArgumentParser(
        description="5-Stage Robust Spec Generation Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Stages:
  1 - First-Party Extraction (slither, drops OZ/libraries)
  2 - Invariant Mining (LLM judgment on state variables)
  3 - Per-Contract Rule Writing (parametric rules for modifier cohorts)
  4 - Adversarial Critic (sibling-pattern bug detection)
  5 - Prover + Vacuity Check (certoraRun)

Examples:
  python -m spec_pipeline ./contracts/PoolFactory.sol
  python -m spec_pipeline ./contracts/PoolFactory.sol --stages 1,2
  python -m spec_pipeline ./my-project --stages 1,2,3,4 --output-dir ./specs
  python -m spec_pipeline ./contract.sol --stage 5 --certora-args "--rule timeout=60"
        """
    )

    parser.add_argument(
        "path",
        help="Path to .sol file or directory containing Solidity files"
    )
    parser.add_argument(
        "--stages", "-s",
        default="1,2,3,4,5",
        help="Comma-separated stages to run (default: all)"
    )
    parser.add_argument(
        "--stage",
        type=int,
        help="Run a single stage by number (overrides --stages)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        help="Output directory (default: spec_pipeline/Output)"
    )
    parser.add_argument(
        "--certora-args",
        default="",
        help="Extra arguments for certoraRun in Stage 5"
    )
    parser.add_argument(
        "--iterative-stage3",
        action="store_true",
        help="Run Stage 3 with certoraRun feedback loop (iterative fixing)"
    )
    parser.add_argument(
        "--typecheck-only",
        action="store_true",
        help=(
            "Use the keyless local CVL typecheck as the feedback signal and "
            "STOP at a clean typecheck; never run the cloud rule-proof even if "
            "CERTORAKEY is set. Implies the keyless local typecheck; no Certora "
            "key required. Also implies the iterative Stage 3 feedback loop "
            "(failures are fed back and fixed), so --iterative-stage3 need not "
            "be passed alongside it."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Max iterations for iterative Stage 3 (default: 3)"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Run every requested stage from its inputs and leave on-disk "
            "artifacts unread, overwriting each stage's artifact (Requirement "
            "3.7). Default off, preserving pre-work-item behavior (R1.6)."
        ),
    )
    parser.add_argument(
        "--require-cache",
        action="store_true",
        help=(
            "Exit with code 3, naming the artifact, when a requested stage's "
            "prerequisite artifact is absent or stale; runs no stage and issues "
            "no LLM call (Requirement 3.8). Default off (R1.6)."
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )

    args = parser.parse_args()

    path = Path(args.path).resolve()
    if not path.exists():
        _emit(f"Error: Path does not exist: {path}", err=True)
        # A bad input path is not a classified verification outcome; treat it as
        # an unexpected terminal state (Requirement 20.10).
        sys.exit(exit_code_for(Outcome.ERROR))

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None

    # Parse stages
    if args.stage:
        stages = [args.stage]
    else:
        try:
            stages = [int(s.strip()) for s in args.stages.split(",")]
        except ValueError:
            _emit(f"Error: Invalid stages format: {args.stages}", err=True)
            sys.exit(exit_code_for(Outcome.ERROR))

    certora_args = args.certora_args.split() if args.certora_args else None

    try:
        if len(stages) == 1:
            # Single stage
            result = run_single_stage(
                stage=stages[0],
                sol_path=path,
                output_dir=output_dir,
                iterative_stage3=args.iterative_stage3,
                certora_args=certora_args,
                max_iterations=args.max_iterations,
                no_cache=args.no_cache,
                require_cache=args.require_cache,
                typecheck_only=args.typecheck_only,
            )
            # A single-stage run may return a Verification_Report (stage 5) or a
            # stage-specific value; derive the outcome when one is present.
            outcome = _outcome_from_results(result)
            if outcome is None and isinstance(result, dict) and result.get("status"):
                outcome = result["status"]
        else:
            # Full pipeline
            results = run_pipeline(
                sol_path=path,
                output_dir=output_dir,
                stages=stages,
                certora_args=certora_args,
                iterative_stage3=args.iterative_stage3,
                max_iterations=args.max_iterations,
                no_cache=args.no_cache,
                require_cache=args.require_cache,
                typecheck_only=args.typecheck_only,
            )
            outcome = _outcome_from_results(results)
    except PipelineOutcome as po:
        # A raised PipelineOutcome carries its own explicit ``exit_code`` (task
        # 4.2's short-circuits and the ``--require-cache`` violation). Honor that
        # code DIRECTLY rather than remapping the outcome through the shared
        # table: exit 3 for a require-cache miss is deliberately not an
        # Outcome_Set exit code (Requirement 3.8), so it must come straight from
        # ``po.exit_code``. ``_finish`` echoes the outcome label alongside it.
        sys.exit(_finish(po.outcome, explicit_code=po.exit_code))
    except Exception as e:
        # A truly unexpected exception maps to ``error`` (exit 12), never 0
        # (Requirement 20.10). Redact secrets before emitting anything.
        _emit(f"Error: {e}", err=True)
        if args.verbose:
            import traceback
            _emit(redact_secrets(traceback.format_exc()), err=True)
        sys.exit(exit_code_for(Outcome.ERROR))

    sys.exit(_finish(outcome))


if __name__ == "__main__":
    main()
