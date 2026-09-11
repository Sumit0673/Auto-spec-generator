"""
Stage 3 Iterative: Repair_Loop with Certora feedback (design: R22).

Runs Stage 3 -> certoraRun -> analyses diagnostics -> re-prompts Stage 3 with
the prover's findings, bounded by ``max_iterations``.

This rewrite fixes two defects the previous loop shipped:

1. **It returned the LAST iteration.** A later iteration can typecheck worse,
   or pass fewer rules, than an earlier one. The loop now returns the BEST
   iteration: the one holding the highest count of passing non-vacuous rules,
   ties broken toward the earliest iteration (R22.1, R22.2). An iteration whose
   status is ``typecheck_failed`` / ``tool_unavailable`` / ``not_run`` counts as
   zero passing rules (R22.1, R22.8), and a typecheck regression after an
   accepted iteration returns the earlier accepted spec (R22.3).

2. **It made an EXTRA post-loop certoraRun call.** certoraRun is now invoked at
   most once per iteration (R22.6) and the returned report is the one produced
   for the selected iteration (R22.5) -- there is no fresh verify after the loop.

The R22 decision logic lives in pure, slither-free helpers
(:func:`_select_best_iteration`, :func:`_passing_non_vacuous_count`,
:func:`_diagnostic_set`, :func:`_diagnostics_repeat`, :func:`_all_rules_passed`,
:func:`_has_vacuous`) so a test can exercise it without slither or certoraRun.
The loop body itself calls :func:`verify_with_prover`, which needs certoraRun, so
the end-to-end loop is not unit-tested here; the pure helpers carry the tested
surface.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .stage1_extract import Stage1Table
from .methods_block import generate_methods_block
from .cvl import extract_cvl, validate_cvl, CVLDiagnostic
from .utils import read_source as _read_source
import os

from .stage5_verify import verify_with_prover, local_typecheck
from .llm_client import get_llm_client
from .prompts import (
    STAGE3_SYSTEM,
    STAGE3_FEEDBACK_SYSTEM,
    format_stage3_user,
    format_stage3_feedback_user,
)


DEFAULT_MAX_ITERATIONS = 3
MAX_ITERATIONS = DEFAULT_MAX_ITERATIONS
_MIN_MAX_ITERATIONS = 1
_MAX_MAX_ITERATIONS = 10

# Statuses that mean "no usable verdict"; such an iteration counts as zero
# passing non-vacuous rules for selection purposes (R22.1, R22.8).
# ``setup_failed`` (certoraRun failed before CVL typechecking) also carries no
# usable verdict.
_NO_VERDICT_STATUSES = frozenset(
    {"typecheck_failed", "setup_failed", "tool_unavailable", "not_run"}
)


_METHODS_BLOCK_RE = re.compile(r"\bmethods\s*\{")


def _has_methods_block(spec: str) -> bool:
    """True when *spec* already contains a ``methods {`` block.

    Used to decide whether the loop should prepend the generated methods block:
    the model's response often already includes one (the prompt shows it), so
    prepending unconditionally would duplicate it.
    """
    return _METHODS_BLOCK_RE.search(spec or "") is not None


def _clamp_max_iterations(max_iterations: int) -> int:
    """Clamp the configured max into the accepted range (default 3, 1-10)."""
    try:
        value = int(max_iterations)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ITERATIONS
    if value < _MIN_MAX_ITERATIONS:
        return _MIN_MAX_ITERATIONS
    if value > _MAX_MAX_ITERATIONS:
        return _MAX_MAX_ITERATIONS
    return value


# ---------------------------------------------------------------------------
# Pure R22 decision helpers (slither-free, certoraRun-free)
# ---------------------------------------------------------------------------


def _iter_status(iteration: dict) -> str:
    return iteration.get("status", "not_run")


def _passing_non_vacuous_count(iteration: dict) -> int:
    """Count of passing non-vacuous rules in one iteration record (R22.1, R22.8).

    An iteration whose status is ``typecheck_failed`` / ``tool_unavailable`` /
    ``not_run`` yields zero regardless of any rule list it happens to carry.
    """
    if _iter_status(iteration) in _NO_VERDICT_STATUSES:
        return 0
    count = 0
    for rule in iteration.get("rules", []):
        if rule.get("status", "") == "PASSED":
            count += 1
    return count


def _typecheck_accepted(iteration: dict) -> bool:
    """True when the CVL typechecker accepted this iteration's spec (R22.3).

    ``typecheck_failed`` is the only status that means an outright typecheck
    rejection. Every other status (including ``tool_unavailable`` / ``not_run``)
    is treated as "not typecheck-rejected", since the typechecker did not reject
    the spec in those cases.
    """
    return _iter_status(iteration) != "typecheck_failed"


def _all_rules_passed(iteration: dict) -> bool:
    """True when the iteration has >=1 rule and every rule PASSED."""
    if _iter_status(iteration) in _NO_VERDICT_STATUSES:
        return False
    rules = iteration.get("rules", [])
    if not rules:
        return False
    return all(r.get("status", "") == "PASSED" for r in rules)


def _has_vacuous(iteration: dict) -> bool:
    """True when the iteration reports at least one vacuous/dead rule."""
    for rule in iteration.get("rules", []):
        if rule.get("status", "") in ("VACUOUS", "DEAD"):
            return True
    return False


def _diagnostic_set(diagnostics) -> frozenset:
    """The order-independent CVL_Diagnostic set: (category, line, message) keys.

    Accepts either :class:`CVLDiagnostic` instances or plain dicts/tuples so the
    helper can be exercised by tests without constructing pipeline objects.
    """
    out = set()
    for d in diagnostics or ():
        if isinstance(d, CVLDiagnostic):
            out.add((d.category, d.line, d.message))
        elif isinstance(d, dict):
            out.add((d.get("category"), d.get("line"), d.get("message")))
        else:  # tuple/list-like
            out.add(tuple(d))
    return frozenset(out)


def _diagnostics_repeat(prev_diagnostics, curr_diagnostics) -> bool:
    """True when two diagnostic sets are equal (order-independent, R22.10)."""
    return _diagnostic_set(prev_diagnostics) == _diagnostic_set(curr_diagnostics)


def _typecheck_error_set(errors) -> frozenset:
    """The order-independent KEYLESS typecheck error set.

    In the keyless path the signal that reflects real progress is the local CVL
    typechecker output (``file:line:col:message``), NOT the constant
    CVL_Validator diagnostics. Two iterations "repeat" only when they produce the
    same set of typecheck errors.

    Each error is normalized to a ``(line, col, message)`` key. ``file`` is
    deliberately excluded: the spec file path is stable across iterations and
    carries no progress signal, whereas ``(line, col, message)`` captures what
    the typechecker actually complained about and where. Comparing this way is
    order-independent (a set) and robust to the typechecker reordering findings.

    Accepts a list of dicts (the loop's ``typecheck_errors`` shape) or anything
    dict-like; unknown shapes fall back to their tuple form so the helper is
    test-friendly.
    """
    out = set()
    for e in errors or ():
        if isinstance(e, dict):
            out.add((e.get("line"), e.get("col"), e.get("message")))
        else:  # tuple/list-like
            out.add(tuple(e))
    return frozenset(out)


def _typecheck_errors_repeat(prev_errors, curr_errors) -> bool:
    """True when two keyless typecheck error sets are equal (order-independent).

    A genuine stall: the typechecker flagged exactly the same errors two
    iterations running, so the loop is making no progress and should stop.
    """
    return _typecheck_error_set(prev_errors) == _typecheck_error_set(curr_errors)


def _select_best_iteration(iterations: list[dict]) -> tuple[int, str]:
    """Select the best iteration index and a human-readable selection reason.

    Rules (R22.1, R22.2, R22.3, R22.8):

    * The best iteration is the one holding the highest count of passing
      non-vacuous rules; a no-verdict iteration counts as zero.
    * Ties are broken toward the earliest (lowest-numbered) iteration.
    * Typecheck regression: while an earlier iteration was typecheck-accepted,
      if a later iteration is typecheck-rejected, return the earlier accepted
      spec and name both iteration numbers in the reason.

    Returns ``(index, reason)`` where ``index`` is the 0-based position in
    ``iterations``. Raises ``ValueError`` on an empty list.
    """
    if not iterations:
        raise ValueError("cannot select a best iteration from zero iterations")

    # Typecheck regression (R22.3): the first typecheck-rejected iteration that
    # follows at least one typecheck-accepted iteration pins the return to that
    # earlier accepted iteration.
    last_accepted_index: Optional[int] = None
    for idx, it in enumerate(iterations):
        if _typecheck_accepted(it):
            last_accepted_index = idx
        else:
            if last_accepted_index is not None:
                earlier_num = iterations[last_accepted_index].get(
                    "iteration", last_accepted_index + 1
                )
                later_num = it.get("iteration", idx + 1)
                reason = (
                    f"typecheck regression: iteration {later_num} was rejected "
                    f"by the CVL typechecker while iteration {earlier_num} was "
                    f"accepted; returning iteration {earlier_num}"
                )
                return last_accepted_index, reason

    # Best-by-count with earliest-wins tie-break (R22.1, R22.2).
    best_index = 0
    best_count = _passing_non_vacuous_count(iterations[0])
    for idx in range(1, len(iterations)):
        count = _passing_non_vacuous_count(iterations[idx])
        if count > best_count:
            best_index = idx
            best_count = count

    best_num = iterations[best_index].get("iteration", best_index + 1)
    reason = (
        f"iteration {best_num} holds the highest count of passing non-vacuous "
        f"rules ({best_count}); ties resolved toward the earliest iteration"
    )
    return best_index, reason


# ---------------------------------------------------------------------------
# The loop (needs certoraRun via verify_with_prover)
# ---------------------------------------------------------------------------


def _diagnostics_to_json(diagnostics) -> list[dict]:
    """Render a diagnostic list as JSON-friendly dicts for the history artifact."""
    out = []
    for d in diagnostics or ():
        if isinstance(d, CVLDiagnostic):
            out.append(
                {"category": d.category, "line": d.line, "message": d.message}
            )
        elif isinstance(d, dict):
            out.append(d)
        else:  # tuple/list-like
            out.append({"value": list(d)})
    return out


def _has_certora_key() -> bool:
    """True when a CERTORAKEY is present in the environment (cloud verdict path).

    The keyless local typecheck never reads this; it only decides whether the
    loop ALSO asks certoraRun for a cloud verdict after a clean local typecheck.
    """
    return bool(os.environ.get("CERTORAKEY"))


def _typecheck_errors_to_iteration_errors(typecheck_errors: list[dict]) -> list[dict]:
    """Render local-typecheck diagnostics as the loop's ``errors`` shape.

    The loop's ``errors`` list normally holds ``{rule, status}`` entries from a
    cloud verdict; for a keyless typecheck failure there are no rule verdicts, so
    each typechecker diagnostic becomes a ``TYPECHECK`` error carrying its
    location and message. This is what feeds the next iteration's feedback prompt.
    """
    out: list[dict] = []
    for e in typecheck_errors:
        out.append(
            {
                "rule": "(typecheck)",
                "status": "TYPECHECK",
                "file": e.get("file"),
                "line": e.get("line"),
                "col": e.get("col"),
                "message": e.get("message"),
            }
        )
    return out


def _synthetic_report(status: str, note: str, local_tc: dict) -> dict:
    """Build a minimal Verification_Report for a keyless typecheck outcome.

    Used when the loop has no cloud verdict to attach (keyless mode): the report
    carries the honest status, an empty rule list, and the local typecheck result
    so the history artifact records what happened.
    """
    return {
        "status": status,
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
        "note": note,
        "local_typecheck": local_tc,
        "local_typecheck_passed": bool(local_tc.get("typecheck_passed")),
    }


def _verify_iteration(
    *,
    source_path,
    current_spec: str,
    output_dir,
    certora_args,
    table,
    use_local_typecheck: bool,
    local_typecheck_fn,
    verify_fn,
    force_keyless: bool = False,
) -> tuple[dict, list[dict], bool]:
    """Run one iteration's verification, keyless-first.

    Returns ``(report, typecheck_errors, typecheck_clean)``:

    * When ``use_local_typecheck`` is set, run the keyless local CVL typecheck
      first. A ``typecheck_failed`` result short-circuits WITHOUT a cloud call and
      returns the parsed diagnostics; a ``setup_failed`` result (certoraRun failed
      BEFORE CVL typechecking -- e.g. an unknown ``--verify`` target or a compile
      error) short-circuits WITHOUT a cloud call and is NOT feedable as a CVL fix;
      a ``typecheck_unavailable`` result falls back to the cloud verifier
      (preserving the pre-keyless behavior and the injected-fake tests); a clean
      ``typecheck_passed`` result either asks the cloud verifier for a real
      verdict (when ``CERTORAKEY`` is set) or is itself the pass.
    * When ``use_local_typecheck`` is False, only the cloud verifier runs.

    certoraRun is invoked at most once per iteration (R22.6): the local typecheck
    IS the certoraRun invocation in keyless mode, and the cloud call is made only
    when a key is present (so at most one certoraRun call total per iteration).
    """
    if use_local_typecheck:
        local_tc = local_typecheck_fn(
            source_path, current_spec, output_dir, table=table
        )
        tc_status = local_tc.get("status")
        tc_errors = list(local_tc.get("errors", []))

        if tc_status == "typecheck_failed":
            report = _synthetic_report(
                "typecheck_failed",
                "keyless local CVL typecheck rejected the spec",
                local_tc,
            )
            report["errors"] = _typecheck_errors_to_iteration_errors(tc_errors)
            report["typecheck_diagnostics"] = tc_errors
            return report, tc_errors, False

        if tc_status == "setup_failed":
            # certoraRun failed BEFORE the CVL typechecker (unknown --verify
            # target, compile error, bad argument). This is NOT a CVL typecheck
            # failure and NOT feedable as a CVL fix. Surface it with the real
            # reason so the loop can stop honestly instead of stalling on empty
            # error sets. No cloud call.
            reason = local_tc.get("reason", "certoraRun setup/compile failure")
            report = _synthetic_report(
                "setup_failed",
                f"keyless local CVL typecheck could not run: {reason}",
                local_tc,
            )
            report["setup_failed_reason"] = reason
            return report, [], False

        if tc_status == "typecheck_passed":
            # ``force_keyless`` gates the key check: when set, the loop behaves
            # as if no key is present regardless of CERTORAKEY, so a clean local
            # typecheck is the terminal pass and the cloud verdict is never
            # requested (``--typecheck-only`` mode).
            if not force_keyless and _has_certora_key():
                # Local checks passed; ask the cloud for the real verdict.
                report = verify_fn(
                    source_path, current_spec, output_dir, certora_args,
                    table=table,
                )
                report.setdefault("local_typecheck", local_tc)
                report["local_typecheck_passed"] = True
                return report, [], True
            # Keyless: a clean local typecheck is the pass.
            return (
                _synthetic_report(
                    "typecheck_passed",
                    "keyless local CVL typecheck passed clean",
                    local_tc,
                ),
                [],
                True,
            )

        # typecheck_unavailable (no certoraRun / no Java >=19): fall back to the
        # cloud verifier exactly as the pre-keyless loop did.

    # Cloud path (local typecheck disabled or unavailable). The keyless
    # "typecheck clean" stop condition does NOT apply here: the rule-verdict stop
    # conditions (all rules passed / repeated diagnostics / max iterations)
    # govern this path, so ``typecheck_clean`` is reported False and any
    # typecheck diagnostics come from the cloud report itself.
    report = verify_fn(
        source_path, current_spec, output_dir, certora_args, table=table
    )
    tc_diags = report.get("typecheck_diagnostics") or []
    return report, list(tc_diags), False


def write_rules_iterative(
    table: Stage1Table,
    invariants: dict,
    source_path: str | Path,
    output_dir: str | Path | None = None,
    llm_client=None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    certora_args: list[str] | None = None,
    *,
    use_local_typecheck: bool = True,
    local_typecheck_fn=None,
    verify_fn=None,
    force_keyless: bool = False,
) -> dict:
    """
    Stage 3 with the iterative Repair_Loop (R22), keyless by default.

    The per-iteration feedback signal is the KEYLESS local CVL typecheck
    (:func:`spec_pipeline.stage5_verify.local_typecheck`): generate a spec, run
    the local typecheck, and when it reports errors, feed the concrete
    ``file:line:col: message`` diagnostics back into the Stage 3 feedback prompt
    and retry. A clean local typecheck is the success/stop condition -- the
    keyless analogue of "all rules passed" when no cloud key is available.

    The cloud path is preserved: when ``CERTORAKEY`` is set (and the local
    typecheck passes / is unavailable), :func:`verify_with_prover` still produces
    a real cloud verdict. When the local typecheck is UNAVAILABLE (no certoraRun
    or no Java >=19), the loop falls back to the cloud verifier exactly as the
    pre-keyless loop did, so callers/tests that inject a fake ``verify_with_prover``
    keep working unchanged.

    Parameters
    ----------
    use_local_typecheck:
        When True (default) the keyless local typecheck is the primary feedback
        signal; when False the loop uses only :func:`verify_with_prover` (the
        pre-keyless behavior).
    local_typecheck_fn, verify_fn:
        Injection seams for hermetic tests: substitutes for
        :func:`local_typecheck` and :func:`verify_with_prover` so the loop runs
        without a real certoraRun/java/LLM.
    force_keyless:
        When True, the loop forces the KEYLESS path regardless of CERTORAKEY:
        every stop/verdict decision behaves as if no key is present, so a clean
        local typecheck is the terminal pass and the cloud verifier
        (:func:`verify_with_prover`) is NEVER called for a verdict. This backs
        ``--typecheck-only`` mode (the cloud rule-proof is deferred until the
        spec typechecks clean). Implemented purely by gating the in-process key
        check -- it never mutates ``os.environ``.

    Returns a dict keeping the keys existing callers use:

    * ``final_spec``  -- the SELECTED (best) iteration's spec (R22.1-R22.3).
    * ``iterations``  -- per-iteration history (spec, diagnostics, verdicts,
      status, passing count) plus the selection reason (R22.7).
    * ``final_report`` -- the SELECTED iteration's report summary, reused
      without any extra post-loop certoraRun call (R22.5, R22.6).
    """
    if llm_client is None:
        llm_client = get_llm_client()

    _local_typecheck = local_typecheck_fn or local_typecheck
    _verify = verify_fn or verify_with_prover

    # In ``force_keyless`` mode every stop/verdict decision behaves as if no key
    # is present (``--typecheck-only``): a clean local typecheck is the terminal
    # pass and no cloud verdict is ever requested. Otherwise the real
    # environment key gates the cloud path exactly as before.
    def _key_present() -> bool:
        return (not force_keyless) and _has_certora_key()

    max_iterations = _clamp_max_iterations(max_iterations)

    source_code = _read_source(source_path)
    stage1_text = table.to_text()
    methods_block = generate_methods_block(table, source_code).text

    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    base_name = Path(source_path).stem if Path(source_path).is_file() else "project"

    iterations: list[dict] = []
    current_spec = ""
    previous_errors = None
    previous_typecheck_errors: Optional[list[dict]] = None
    stop_cause: Optional[str] = None

    for iteration in range(1, max_iterations + 1):
        print(f"\n{'='*60}")
        print(f"Stage 3 Iteration {iteration}/{max_iterations}")
        print(f"{'='*60}")

        # Build user prompt with error context from the previous iteration. The
        # concrete CVL typechecker errors (file:line:col: message) from the
        # keyless local typecheck are fed back so the LLM can fix the exact
        # location (e.g. a missing `sig:` selector).
        if iteration == 1:
            user_prompt = format_stage3_user(
                stage1_text, invariants, source_code, methods_block
            )
        else:
            user_prompt = format_stage3_feedback_user(
                stage1_text, invariants, source_code, methods_block,
                current_spec, previous_errors,
                typecheck_errors=previous_typecheck_errors,
            )

        response = llm_client.call(
            STAGE3_SYSTEM if iteration == 1 else STAGE3_FEEDBACK_SYSTEM,
            user_prompt, temperature=0.2,
        )

        # Extract the model's CVL first WITHOUT prepending, then prepend the
        # generated methods block ONLY when the extracted CVL does not already
        # carry one. The Stage 3 prompt shows the methods block ("use these
        # exact signatures"), so the model frequently emits its own `methods {`
        # block; unconditionally prepending the generated block then produces a
        # SECOND methods block and a persistent `duplicated_methods_block`
        # diagnostic. Deduplicating here (the loop layer) keeps
        # ``cvl.extract_cvl``'s documented round-trip/idempotence contract intact.
        extracted = extract_cvl(response)
        if _has_methods_block(extracted):
            current_spec = extracted
        else:
            current_spec = extract_cvl(response, methods_block)

        # CVL_Diagnostic set for the repeated-diagnostics stop condition
        # (R22.10). Computed via the shared CVL_Validator (design: R18/R22).
        diagnostics = validate_cvl(current_spec, methods_block)

        # Save intermediate spec.
        if output_dir:
            iter_spec_file = output_dir / f"{base_name}_stage3_iter{iteration}.cvl"
            iter_spec_file.write_text(current_spec)
            print(f"Saved iteration {iteration} spec to {iter_spec_file}")

        # --- verification: KEYLESS local typecheck as the primary signal ----
        # (AT MOST ONE certoraRun invocation per iteration, R22.6).
        report, typecheck_errors, typecheck_clean = _verify_iteration(
            source_path=source_path,
            current_spec=current_spec,
            output_dir=output_dir,
            certora_args=certora_args,
            table=table,
            use_local_typecheck=use_local_typecheck,
            local_typecheck_fn=_local_typecheck,
            verify_fn=_verify,
            force_keyless=force_keyless,
        )

        status = report.get("status", "not_run")
        rules = report.get("rules", [])
        errors = _extract_errors(report)

        record = {
            "iteration": iteration,
            "spec": current_spec,
            "report": report.get("summary", {}),
            "rules": rules,
            "diagnostics": _diagnostics_to_json(diagnostics),
            "status": status,
            "errors": errors,
            "typecheck_errors": typecheck_errors,
            "typecheck_clean": typecheck_clean,
        }
        record["passing_non_vacuous_count"] = _passing_non_vacuous_count(record)
        record["typecheck_accepted"] = _typecheck_accepted(record)
        iterations.append(record)

        # Save iteration report.
        if output_dir:
            iter_report_file = (
                output_dir / f"{base_name}_stage5_iter{iteration}_report.json"
            )
            with open(iter_report_file, "w") as f:
                json.dump(
                    {"summary": report.get("summary", {}), "rules": rules},
                    f, indent=2, default=str,
                )

        # --- stop conditions ------------------------------------------------

        # (a) certoraRun could not be invoked for this iteration (R22.11):
        # stop, mark the iteration tool_unavailable, record the blocking cause.
        if status == "tool_unavailable":
            record["status"] = "tool_unavailable"
            record["passing_non_vacuous_count"] = 0
            cause = report.get(
                "note", "certoraRun could not be invoked for this iteration"
            )
            searched = report.get("searched_paths") or []
            record["blocking_cause"] = cause
            if searched:
                record["searched_paths"] = searched
            stop_cause = f"prover uninvokable: {cause}"
            print(f"\n! certoraRun unavailable - stopping. {cause}")
            break

        # (a2) certoraRun failed BEFORE the CVL typechecker for this iteration
        # (unknown/mismatched --verify target, compile error, bad argument).
        # This is a blocking setup/compile problem, NOT a feedable CVL error, so
        # stop honestly and surface the real reason instead of iterating on empty
        # error sets (which would otherwise stall on "repeated typecheck errors").
        if status == "setup_failed":
            reason = report.get(
                "setup_failed_reason",
                report.get("note", "certoraRun setup/compile failure"),
            )
            record["blocking_cause"] = reason
            record["setup_failed_reason"] = reason
            stop_cause = f"local typecheck could not run: {reason}"
            print(
                f"\n! Local CVL typecheck could not run at iteration {iteration} "
                f"(setup/compile failure) - stopping. {reason}"
            )
            break

        # (b-keyless) The keyless local typecheck passed clean. With no cloud
        # key this is the success/stop condition, analogous to "all rules
        # passed" -- the spec typechecks, so select it and declare pass.
        if typecheck_clean and not _key_present():
            print(
                f"\n+ Local CVL typecheck PASSED clean at iteration {iteration} "
                "(keyless) - selecting this spec."
            )
            stop_cause = "local typecheck passed (keyless)"
            break

        # (b) Every rule passing and zero vacuous (R22.9) -- the cloud verdict
        # success/stop condition when a key produced real per-rule verdicts.
        if _all_rules_passed(record) and not _has_vacuous(record):
            print(f"\n+ All rules PASSED (non-vacuous) at iteration {iteration}!")
            stop_cause = "all rules passing, zero vacuous"
            break

        # (c) Repeated-signal stop vs the immediately preceding iteration
        # (R22.10). The signal depends on which path is active:
        #
        # * KEYLESS (use_local_typecheck and no CERTORAKEY): the CVL_Validator
        #   diagnostics are a CONSTANT here (e.g. `duplicated_methods_block`) and
        #   do NOT reflect progress. The real signal is the local typecheck error
        #   set (file:line:col:message), which changes as the LLM fixes things.
        #   Only stop when the SAME typecheck errors recur two iterations running
        #   (a genuine stall). While the errors keep changing, keep iterating.
        # * CLOUD (CERTORAKEY present / local typecheck unavailable): the rule
        #   verdicts drive the loop, so keep the pre-existing CVL_Validator
        #   diagnostic comparison unchanged.
        # The keyless typecheck signal is active only when THIS iteration's
        # outcome actually came from the local CVL typecheck (status
        # ``typecheck_failed``). When the local typecheck was unavailable the
        # loop fell back to the cloud verifier, whose status is a rule verdict
        # (e.g. ``violated``/``verified``); that path keeps the CVL_Validator
        # diagnostic comparison below.
        keyless_signal = (
            iteration > 1
            and use_local_typecheck
            and not _key_present()
            and status == "typecheck_failed"
            and iterations[-2].get("status") == "typecheck_failed"
        )
        if keyless_signal:
            if _typecheck_errors_repeat(
                iterations[-2]["typecheck_errors"], record["typecheck_errors"]
            ):
                print(
                    "\n! Typecheck errors repeated vs the previous iteration "
                    "- stopping (no progress)."
                )
                stop_cause = "repeated typecheck errors"
                break
        elif iteration > 1 and _diagnostics_repeat(
            iterations[-2]["diagnostics"], record["diagnostics"]
        ):
            print("\n! Diagnostic set repeated vs the previous iteration - stopping.")
            stop_cause = "repeated CVL diagnostic set"
            break

        previous_errors = errors
        previous_typecheck_errors = typecheck_errors or None

        # (d) Max iterations reached (R22.4).
        if iteration < max_iterations:
            n_tc = len(typecheck_errors or [])
            if n_tc:
                print(f"Typecheck errors found: {n_tc}; retrying with fixes...")
            else:
                print(f"Errors found: {len(errors)} rules failed/vacuous")
                print("Retrying with error context...")
        else:
            print(f"\n! Max iterations ({max_iterations}) reached.")
            stop_cause = f"reached configured max iterations ({max_iterations})"

    # --- select the BEST iteration and reuse its report (R22.1-R22.6) -------
    # Keyless success: when the loop stopped on a clean local typecheck, the
    # spec that typechecked clean IS the selection -- it is the keyless analogue
    # of "all rules passed". Count-based selection would tie it at zero passing
    # rules (a clean typecheck carries no per-rule verdicts) and wrongly resolve
    # to an earlier failing iteration, so pick the clean iteration directly.
    if stop_cause == "local typecheck passed (keyless)" and iterations[-1].get(
        "typecheck_clean"
    ):
        best_index = len(iterations) - 1
        best = iterations[best_index]
        best_num = best.get("iteration", best_index + 1)
        selection_reason = (
            f"iteration {best_num} typechecked clean under the keyless local CVL "
            "typecheck (no cloud key); selected as the keyless pass"
        )
    else:
        best_index, selection_reason = _select_best_iteration(iterations)
        best = iterations[best_index]

    # Record the selection reason on every iteration record, naming the returned
    # iteration number (R22.7).
    returned_num = best.get("iteration", best_index + 1)
    for it in iterations:
        it["selection_reason"] = selection_reason
        it["returned_iteration"] = returned_num

    result = {
        "final_spec": best["spec"],
        "iterations": iterations,
        "final_report": best["report"],
        "selected_iteration": returned_num,
        "selection_reason": selection_reason,
        "stop_cause": stop_cause,
    }

    # Save complete iteration history (R22.7).
    if output_dir:
        history_file = output_dir / f"{base_name}_stage3_iterative_history.json"
        with open(history_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nSaved iterative history to {history_file}")

    return result


def _extract_errors(report: dict) -> list[dict]:
    """Extract actionable errors from a certoraRun report."""
    errors = []
    for rule in report.get("rules", []):
        status = rule.get("status", "")
        if status in ("FAILED", "VACUOUS", "DEAD", "TIMEOUT", "ERROR"):
            error = {
                "rule": rule.get("name", "unknown"),
                "status": status,
            }
            if "vacuity_reason" in rule:
                error["vacuity_reason"] = rule["vacuity_reason"]
            if "dead_reason" in rule:
                error["dead_reason"] = rule["dead_reason"]
            errors.append(error)
    return errors


def _errors_unchanged(prev_errors: list[dict], curr_errors: list[dict]) -> bool:
    """Check if the error set is essentially unchanged (retained for callers)."""
    prev_set = {(e["rule"], e["status"]) for e in prev_errors}
    curr_set = {(e["rule"], e["status"]) for e in curr_errors}
    return prev_set == curr_set
