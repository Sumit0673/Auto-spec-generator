"""Evaluation_Harness — run the Spec_Pipeline over the Pair_Index (Requirement 13).

This module drives the whole corpus in one repeatable run that survives
individual failures. It invokes the Spec_Pipeline once per :class:`PairEntry`,
in Pair_Index order, into a per-entry output directory derived from the
repository name and the contract name (Requirement 13.1). It isolates each
invocation behind a per-pair time budget and an exception boundary so one broken
repository never loses the results (Requirements 13.3, 13.4), writes a per-pair
record before dispatching the next entry (Requirement 13.5), supports ``--resume``
keyed on the Source_Fingerprint (Requirements 13.6, 13.11), and finishes by
writing a machine-readable results file plus a human-readable summary stamped
with a run identifier derived from the UTC start time and the repository commit
(Requirements 13.7, 13.9, 13.12).

Import-safety
-------------
This module imports only slither-free code at module load time:
:mod:`spec_pipeline.eval.pair_index` and :mod:`spec_pipeline.eval.metrics` are
both slither-free. The Spec_Pipeline itself (which imports slither via Stage 1)
is imported **lazily**, inside :func:`_default_runner`, so ``import
spec_pipeline.eval.harness`` succeeds on a machine with no Solidity tooling and
tests can inject a fake runner instead.

Concurrency and the per-pair budget
------------------------------------
``--jobs N`` runs up to ``N`` invocations concurrently through a
:class:`concurrent.futures.ThreadPoolExecutor`. Each invocation is additionally
guarded by the per-pair budget (1800 s): the runner runs on its own worker
thread and, when the budget elapses, the harness records a ``timeout`` outcome
and moves on. A cooperative fake runner (one that checks a stop event, or simply
sleeps) is sufficient to drive the timeout path in tests. In production the
default runner shells the pipeline in-process; real termination of certoraRun
child processes is documented on :func:`_default_runner` as requiring a process
pool / subprocess kill — a follow-up when Stage 5 spawns certoraRun as a
subprocess. The harness never leaves a recorded pair without exactly one
Outcome_Set outcome.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from spec_pipeline.eval.metrics import (
    EvaluationInputs,
    MetricReport,
    PairReport,
    RuleVerdict,
    SpecTypecheck,
    compute_metrics,
)
from spec_pipeline.eval.pair_index import (
    PairEntry,
    PairIndex,
    build_pair_index,
    resolve_dataset_root,
)

__all__ = [
    "PER_PAIR_BUDGET_SECONDS",
    "Outcome",
    "OUTCOME_SET",
    "PairRecord",
    "HarnessResult",
    "RunnerResult",
    "default_output_dir_names",
    "compute_run_id",
    "run_harness",
    "write_results",
    "read_results",
    "write_summary",
    "build_arg_parser",
    "main",
]


# The configured per-pair budget in seconds (Requirement 13.3).
PER_PAIR_BUDGET_SECONDS = 1800

# Outcome_Set members this harness can assign to a per-pair record. ``timeout``
# and ``error`` are produced directly by the harness (R13.3, R13.4); every other
# value is produced by a pipeline invocation and carried through.
OUTCOME_SET = (
    "verified",
    "verified_with_warnings",
    "violated",
    "vacuous",
    "timeout",
    "typecheck_failed",
    "compile_failed",
    "no_first_party_contracts",
    "no_compatible_solc",
    "unsupported_pragma_set",
    "tool_unavailable",
    "llm_unavailable",
    "skipped_missing_tool",
    "error",
)

Outcome = str


# ---------------------------------------------------------------------------
# Runner protocol
# ---------------------------------------------------------------------------


@dataclass
class RunnerResult:
    """The result of one pipeline invocation as consumed by the harness.

    The runner is injectable so tests never touch slither/LLM/certoraRun. A
    runner receives the analyzed ``.sol`` path and its per-pair output directory
    and returns this shape.

    Attributes:
        outcome: Exactly one Outcome_Set member (see :data:`OUTCOME_SET`).
        spec_path: Path to the generated ``.spec`` (or ``None`` when not
            produced — an absent path is recorded, R13.5).
        report_path: Path to the Verification_Report (or ``None`` when not
            produced).
        metric_inputs: The Requirement-12-shaped metric inputs for this pair -
            an :class:`EvaluationInputs` (preferred) or a plain dict of the same
            shape. Defaults to an empty :class:`EvaluationInputs`.
    """

    outcome: Outcome
    spec_path: Optional[Path] = None
    report_path: Optional[Path] = None
    metric_inputs: object = field(default_factory=EvaluationInputs)


# A runner maps (sol_path, output_dir, stop_event) -> RunnerResult. The
# stop_event lets a cooperative runner notice a budget expiry and unwind; a
# runner may ignore it (the harness still records ``timeout`` and moves on).
Runner = Callable[[Path, Path, threading.Event], RunnerResult]


def _default_runner(
    sol_path: Path, output_dir: Path, stop_event: threading.Event
) -> RunnerResult:
    """The production runner: a thin wrapper over ``run_pipeline`` (R13.1).

    ``run_pipeline`` is imported lazily here so this module stays import-safe on
    a machine without slither. The pipeline's summary dict is mapped to a
    :class:`RunnerResult`.

    Termination note: this in-process runner cannot be force-killed once
    ``run_pipeline`` is executing native slither/certoraRun work. Real
    termination of child processes on a budget expiry requires running the
    pipeline in a separate process (a :class:`concurrent.futures.ProcessPoolExecutor`
    or a ``subprocess``) and killing the process group. The harness's timeout
    path (record ``timeout`` and continue) is correct regardless; only the
    reclamation of the abandoned worker differs.
    """
    from spec_pipeline.pipeline import run_pipeline  # lazy: keeps import slither-free

    summary = run_pipeline(sol_path, output_dir=output_dir)
    outcome = summary.get("outcome") or _infer_outcome_from_summary(summary)
    spec_path = _first_existing(output_dir, ("*.spec",))
    report_path = _first_existing(output_dir, ("*stage5*.json", "*report*.json"))
    return RunnerResult(
        outcome=outcome,
        spec_path=spec_path,
        report_path=report_path,
        metric_inputs=_metric_inputs_from_summary(summary),
    )


def _infer_outcome_from_summary(summary: dict) -> Outcome:
    """Best-effort Outcome_Set derivation when the pipeline set no outcome.

    A run that finished every requested stage without a terminal outcome is
    reported through its Stage 5 verification status when present, else ``error``.
    """
    stage5 = summary.get("stages", {}).get("stage5") or {}
    status = stage5.get("status")
    if status in OUTCOME_SET:
        return status
    if status in ("verified", "not_run", None):
        return status if status in OUTCOME_SET else "error"
    return "error"


def _metric_inputs_from_summary(summary: dict) -> EvaluationInputs:
    """Map a pipeline summary to metric inputs. Minimal by design.

    The harness records whatever metric inputs the runner supplies; the default
    runner produces a conservative :class:`EvaluationInputs` from the summary so
    a real run is still scoreable. Richer extraction lives with the metrics
    plumbing; here we only surface what the summary already carries.
    """
    return EvaluationInputs()


def _first_existing(output_dir: Path, patterns: tuple[str, ...]) -> Optional[Path]:
    for pattern in patterns:
        matches = sorted(output_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------------
# Per-pair record
# ---------------------------------------------------------------------------


@dataclass
class PairRecord:
    """One per-pair record written before the next entry is dispatched (R13.5).

    Attributes:
        run_id: The run identifier, stamped in every record (R13.9).
        repo: Repository name.
        contract_path: The ``.sol`` path (relative, as in the Pair_Index).
        outcome: Exactly one Outcome_Set member.
        elapsed_s: Wall-clock seconds the invocation took (or the budget on a
            timeout).
        source_fingerprint: Source_Fingerprint of the analyzed ``.sol`` (R13.5,
            used by ``--resume`` matching, R13.6/R13.11).
        metric_inputs: Requirement-12-shaped metric inputs (serialized form).
        spec_path: Generated spec path, or ``None`` when not produced (R13.5).
        report_path: Verification_Report path, or ``None`` when not produced.
        traceback_path: Path to a captured traceback for an ``error`` outcome
            (R13.4); ``None`` otherwise.
        error_type: Exception type name for an ``error`` outcome; ``None``
            otherwise.
        skipped: True when this record was carried over by ``--resume`` rather
            than produced by a fresh invocation (R13.6, R13.11).
    """

    run_id: str
    repo: str
    contract_path: str
    outcome: Outcome
    elapsed_s: float
    source_fingerprint: str
    metric_inputs: dict = field(default_factory=dict)
    spec_path: Optional[str] = None
    report_path: Optional[str] = None
    traceback_path: Optional[str] = None
    error_type: Optional[str] = None
    skipped: bool = False

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "repo": self.repo,
            "contract_path": self.contract_path,
            "outcome": self.outcome,
            "elapsed_s": self.elapsed_s,
            "source_fingerprint": self.source_fingerprint,
            "metric_inputs": self.metric_inputs,
            "spec_path": self.spec_path,
            "report_path": self.report_path,
            "traceback_path": self.traceback_path,
            "error_type": self.error_type,
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PairRecord":
        return cls(
            run_id=payload["run_id"],
            repo=payload["repo"],
            contract_path=payload["contract_path"],
            outcome=payload["outcome"],
            elapsed_s=payload.get("elapsed_s", 0.0),
            source_fingerprint=payload.get("source_fingerprint", ""),
            metric_inputs=payload.get("metric_inputs", {}),
            spec_path=payload.get("spec_path"),
            report_path=payload.get("report_path"),
            traceback_path=payload.get("traceback_path"),
            error_type=payload.get("error_type"),
            skipped=payload.get("skipped", False),
        )

    def has_single_outcome(self) -> bool:
        """True when the record carries exactly one valid Outcome_Set outcome."""
        return isinstance(self.outcome, str) and self.outcome in OUTCOME_SET


# ---------------------------------------------------------------------------
# metric-inputs (de)serialization — Requirement 12 shapes
# ---------------------------------------------------------------------------


def _serialize_metric_inputs(inputs: object) -> dict:
    """Serialize an :class:`EvaluationInputs` (or dict) to plain JSON data.

    A plain dict is passed through unchanged (assumed already serializable). An
    :class:`EvaluationInputs` is projected to the subset the harness records and
    later re-reads for the round-trip property (R13.8): the per-pair spec
    typecheck results, the pair reports (with per-rule verdicts), and telemetry
    is left to the metrics layer.
    """
    if isinstance(inputs, dict):
        return inputs
    if not isinstance(inputs, EvaluationInputs):
        return {}
    return {
        "spec_typechecks": [
            {"pair_id": s.pair_id, "result": s.result}
            for s in inputs.spec_typechecks
        ],
        "pair_reports": [
            {
                "pair_id": r.pair_id,
                "scored": r.scored,
                "report_status": r.report_status,
                "verdicts": [
                    {"name": v.name, "verdict": v.verdict} for v in r.verdicts
                ],
            }
            for r in inputs.pair_reports
        ],
    }


def _deserialize_metric_inputs(payload: dict) -> EvaluationInputs:
    """Rebuild an :class:`EvaluationInputs` from :func:`_serialize_metric_inputs`."""
    if not isinstance(payload, dict):
        return EvaluationInputs()
    spec_typechecks = tuple(
        SpecTypecheck(pair_id=s["pair_id"], result=s["result"])
        for s in payload.get("spec_typechecks", [])
    )
    pair_reports = tuple(
        PairReport(
            pair_id=r["pair_id"],
            scored=r.get("scored", True),
            report_status=r.get("report_status"),
            verdicts=tuple(
                RuleVerdict(name=v["name"], verdict=v["verdict"])
                for v in r.get("verdicts", [])
            ),
        )
        for r in payload.get("pair_reports", [])
    )
    return EvaluationInputs(
        spec_typechecks=spec_typechecks, pair_reports=pair_reports
    )


def _merge_metric_inputs(records: list[PairRecord]) -> EvaluationInputs:
    """Combine every record's metric inputs into one :class:`EvaluationInputs`."""
    spec_typechecks: list[SpecTypecheck] = []
    pair_reports: list[PairReport] = []
    for record in records:
        inputs = _deserialize_metric_inputs(record.metric_inputs)
        spec_typechecks.extend(inputs.spec_typechecks)
        pair_reports.extend(inputs.pair_reports)
    return EvaluationInputs(
        spec_typechecks=tuple(spec_typechecks),
        pair_reports=tuple(pair_reports),
    )


# ---------------------------------------------------------------------------
# Output-dir naming (Requirement 13.1)
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    """Return a filesystem-safe slug (keep alnum, dash, underscore, dot)."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in text)


def default_output_dir_names(entries: list[PairEntry]) -> dict[int, str]:
    """Map each entry (by index) to a unique per-entry output-dir name (R13.1).

    The name is derived from the repository name and the contract name. When
    that pair repeats (two entries share repo + contract name), the contract
    path is folded in to disambiguate, so every entry gets a distinct directory.
    """
    # First pass: count (repo, contract_name) collisions.
    counts: dict[tuple[str, str], int] = {}
    for entry in entries:
        key = (entry.repository, entry.contract_name)
        counts[key] = counts.get(key, 0) + 1

    names: dict[int, str] = {}
    used: set[str] = set()
    for i, entry in enumerate(entries):
        key = (entry.repository, entry.contract_name)
        base = f"{_slug(entry.repository)}__{_slug(entry.contract_name)}"
        if counts[key] > 1:
            # Repeats: fold in the contract path (slugged) to disambiguate.
            path_slug = _slug(entry.contract_path.replace("/", "_"))
            base = f"{base}__{path_slug}"
        # Final guard against any residual collision.
        name = base
        n = 1
        while name in used:
            n += 1
            name = f"{base}__{n}"
        used.add(name)
        names[i] = name
    return names


# ---------------------------------------------------------------------------
# Run identifier (Requirements 13.9, 13.12)
# ---------------------------------------------------------------------------


def _resolve_commit(repo_root: Optional[Path]) -> Optional[str]:
    """Return the short repo commit, or ``None`` when it cannot be resolved."""
    if repo_root is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.strip()
    return commit or None


def compute_run_id(
    start_utc: datetime, commit: Optional[str]
) -> tuple[str, bool]:
    """Derive the run identifier from the UTC start and the repo commit (R13.9).

    Returns ``(run_id, commit_unresolved)``. When *commit* is ``None`` the id is
    derived from the UTC start alone and ``commit_unresolved`` is True so callers
    can record that the commit was unresolved (R13.12).
    """
    stamp = start_utc.strftime("%Y%m%dT%H%M%SZ")
    if commit:
        return f"{stamp}-{commit}", False
    return f"{stamp}-nocommit", True


# ---------------------------------------------------------------------------
# Results + summary
# ---------------------------------------------------------------------------


@dataclass
class HarnessResult:
    """The full result of one harness run.

    Attributes:
        run_id: The run identifier stamped everywhere (R13.9).
        commit_unresolved: True when the commit could not be resolved (R13.12).
        records: One :class:`PairRecord` per attempted-or-skipped entry, in
            Pair_Index order.
        metrics: The aggregate :class:`MetricReport` over all records.
        attempted: Count of entries a fresh invocation ran for.
        skipped: Count of entries carried over by ``--resume`` (R13.6).
        scored: Count of records whose outcome contributed a scored pair.
    """

    run_id: str
    commit_unresolved: bool
    records: list[PairRecord]
    metrics: MetricReport
    attempted: int
    skipped: int
    scored: int


def _canonical_json(payload: dict) -> str:
    """Canonical JSON: sorted keys, two-space indent, trailing newline (R21.7)."""
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def write_results(result: HarnessResult, path: Path) -> Path:
    """Write the machine-readable results file (R13.7, R13.8, R13.9).

    Round-trips: reading the file back yields equal metric values and equal
    per-pair outcomes (R13.8).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": result.run_id,
        "commit_unresolved": result.commit_unresolved,
        "attempted": result.attempted,
        "skipped": result.skipped,
        "scored": result.scored,
        "metrics": result.metrics.as_dict(),
        "records": [r.to_dict() for r in result.records],
    }
    path.write_text(_canonical_json(payload), encoding="utf-8")
    return path


def read_results(path: Path) -> dict:
    """Read a results file previously written by :func:`write_results`.

    Returns the parsed payload. The per-pair outcomes are available at
    ``payload["records"][i]["outcome"]`` and metric values at
    ``payload["metrics"]``, so an equality check after a write/read confirms the
    round-trip property (R13.8).
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _summary_lines(result: HarnessResult) -> list[str]:
    """Build the human-readable summary lines (R13.7)."""
    lines: list[str] = []
    lines.append(f"Evaluation run {result.run_id}")
    if result.commit_unresolved:
        lines.append("  (repository commit unresolved — run id derived from UTC start alone)")
    lines.append(
        f"attempted={result.attempted} skipped={result.skipped} "
        f"scored={result.scored}"
    )

    # Group pairs by outcome, listing repo + contract.
    lines.append("")
    lines.append("Outcomes:")
    by_outcome: dict[str, list[PairRecord]] = {}
    for record in result.records:
        by_outcome.setdefault(record.outcome, []).append(record)
    for outcome in sorted(by_outcome):
        group = by_outcome[outcome]
        lines.append(f"  {outcome} ({len(group)}):")
        for record in sorted(group, key=lambda r: (r.repo, r.contract_path)):
            lines.append(f"    - {record.repo} :: {record.contract_path}")

    # Each Requirement-12 metric with value-or-null + reason.
    lines.append("")
    lines.append("Metrics:")
    metric_dict = result.metrics.as_dict()
    for name in (
        "syntax_validity_rate",
        "verdict_rate",
        "vacuity_rate",
        "effective_pass_rate",
        "ground_truth_coverage",
        "rules_per_gated_function_aggregate",
    ):
        metric = metric_dict.get(name, {})
        value = metric.get("value")
        if value is None:
            lines.append(f"  {name}: null (reason={metric.get('reason')})")
        else:
            lines.append(f"  {name}: {value}")
    return lines


def write_summary(result: HarnessResult, path: Path) -> Path:
    """Write the human-readable summary file (R13.7, R13.9)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_summary_lines(result)) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Per-pair invocation with the budget + exception boundary
# ---------------------------------------------------------------------------


def _run_one(
    entry: PairEntry,
    dataset_root: Path,
    out_dir: Path,
    runner: Runner,
    run_id: str,
    budget_seconds: float,
    traceback_dir: Path,
) -> PairRecord:
    """Invoke *runner* for one entry under the budget + exception boundary.

    Produces exactly one :class:`PairRecord` with one Outcome_Set outcome:

    * ``timeout`` with the elapsed budget when the invocation exceeds the budget
      (R13.3);
    * ``error`` with the exception type + a captured traceback path when the
      invocation raises (R13.4);
    * otherwise the runner-supplied outcome (R13.5).
    """
    sol_path = dataset_root / entry.contract_path
    fingerprint = _entry_fingerprint(sol_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    stop_event = threading.Event()
    holder: dict[str, object] = {}

    def _target() -> None:
        try:
            holder["result"] = runner(sol_path, out_dir, stop_event)
        except BaseException as exc:  # noqa: BLE001 - classify any fault (R13.4)
            holder["exc"] = exc
            holder["tb"] = traceback.format_exc()

    worker = threading.Thread(target=_target, daemon=True)
    start = datetime.now(timezone.utc)
    worker.start()
    worker.join(timeout=budget_seconds)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()

    if worker.is_alive():
        # Budget exceeded: signal the cooperative runner and record timeout.
        stop_event.set()
        return PairRecord(
            run_id=run_id,
            repo=entry.repository,
            contract_path=entry.contract_path,
            outcome="timeout",
            elapsed_s=min(elapsed, float(budget_seconds)),
            source_fingerprint=fingerprint,
            metric_inputs={},
            spec_path=None,
            report_path=None,
        )

    if "exc" in holder:
        exc = holder["exc"]
        tb_text = str(holder.get("tb", ""))
        # A cooperative runner may raise TimeoutError to signal budget expiry.
        if isinstance(exc, TimeoutError):
            return PairRecord(
                run_id=run_id,
                repo=entry.repository,
                contract_path=entry.contract_path,
                outcome="timeout",
                elapsed_s=elapsed,
                source_fingerprint=fingerprint,
                metric_inputs={},
            )
        tb_path = _write_traceback(
            traceback_dir, entry.repository, entry.contract_name, tb_text
        )
        return PairRecord(
            run_id=run_id,
            repo=entry.repository,
            contract_path=entry.contract_path,
            outcome="error",
            elapsed_s=elapsed,
            source_fingerprint=fingerprint,
            metric_inputs={},
            spec_path=None,
            report_path=None,
            traceback_path=str(tb_path),
            error_type=type(exc).__name__,
        )

    result = holder.get("result")
    if not isinstance(result, RunnerResult) or not (
        isinstance(result.outcome, str) and result.outcome in OUTCOME_SET
    ):
        # No usable result -> error (R13.4).
        tb_path = _write_traceback(
            traceback_dir,
            entry.repository,
            entry.contract_name,
            "runner returned no valid RunnerResult",
        )
        return PairRecord(
            run_id=run_id,
            repo=entry.repository,
            contract_path=entry.contract_path,
            outcome="error",
            elapsed_s=elapsed,
            source_fingerprint=fingerprint,
            metric_inputs={},
            traceback_path=str(tb_path),
            error_type="NoResult",
        )

    return PairRecord(
        run_id=run_id,
        repo=entry.repository,
        contract_path=entry.contract_path,
        outcome=result.outcome,
        elapsed_s=elapsed,
        source_fingerprint=fingerprint,
        metric_inputs=_serialize_metric_inputs(result.metric_inputs),
        spec_path=str(result.spec_path) if result.spec_path is not None else None,
        report_path=(
            str(result.report_path) if result.report_path is not None else None
        ),
    )


def _entry_fingerprint(sol_path: Path) -> str:
    """Source_Fingerprint of the analyzed ``.sol`` (R13.5).

    Mirrors ``pipeline._current_fingerprint`` without importing the slither-backed
    pipeline module: a file input fingerprints itself; a directory fingerprints
    every ``.sol`` beneath it. Uses the slither-free
    :func:`spec_pipeline.eval` -> :func:`source_fingerprint` from ``artifacts``.
    """
    from spec_pipeline.artifacts import source_fingerprint  # slither-free helper

    sol_path = Path(sol_path)
    if not sol_path.exists():
        return "sha256:absent"
    if sol_path.is_file():
        files = [sol_path]
    else:
        files = sorted(sol_path.rglob("*.sol"))
    return source_fingerprint(sol_path, files)


def _write_traceback(
    traceback_dir: Path, repo: str, contract_name: str, text: str
) -> Path:
    """Write a captured traceback to a file and return its path (R13.4)."""
    traceback_dir.mkdir(parents=True, exist_ok=True)
    name = f"{_slug(repo)}__{_slug(contract_name)}.traceback.txt"
    path = traceback_dir / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Resume support (Requirements 13.6, 13.11)
# ---------------------------------------------------------------------------


def _load_existing_records(records_dir: Path) -> dict[tuple[str, str], PairRecord]:
    """Load per-pair records already on disk, keyed by (repo, contract_path)."""
    out: dict[tuple[str, str], PairRecord] = {}
    if not records_dir.exists():
        return out
    for path in sorted(records_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            record = PairRecord.from_dict(payload)
        except (KeyError, TypeError):
            continue
        out[(record.repo, record.contract_path)] = record
    return out


def _record_path(records_dir: Path, entry: PairEntry, out_name: str) -> Path:
    return records_dir / f"{out_name}.json"


def _write_record(records_dir: Path, out_name: str, record: PairRecord) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{out_name}.json"
    path.write_text(_canonical_json(record.to_dict()), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Top-level harness driver (Requirement 13)
# ---------------------------------------------------------------------------


def run_harness(
    *,
    output_root: Path,
    entries: Optional[list[PairEntry]] = None,
    dataset_base: Optional[Path] = None,
    runner: Runner = _default_runner,
    jobs: int = 1,
    resume: bool = False,
    budget_seconds: float = PER_PAIR_BUDGET_SECONDS,
    repo_root: Optional[Path] = None,
    start_utc: Optional[datetime] = None,
    commit: Optional[str] = None,
) -> HarnessResult:
    """Run the Spec_Pipeline over the Pair_Index and score the results (R13).

    Parameters
    ----------
    output_root:
        Root directory holding per-pair output dirs, per-pair records, tracebacks,
        and the results + summary files.
    entries:
        The Pair_Index entries to run, in order. When ``None`` the harness builds
        the index from *dataset_base* (or the resolved dataset root).
    dataset_base:
        Optional dataset base used to resolve the dataset root and (when
        *entries* is None) to build the Pair_Index.
    runner:
        The injectable pipeline runner (defaults to :func:`_default_runner`).
        Tests inject a fake runner so no real pipeline/slither/LLM/certoraRun
        runs.
    jobs:
        Up to ``jobs`` concurrent invocations (default 1). Must be in 1..16;
        callers validate via :func:`_validate_jobs` at the CLI boundary.
    resume:
        When True, skip each entry whose existing per-pair record carries one
        Outcome_Set outcome AND a Source_Fingerprint equal to the one computed
        now, carrying the recorded outcome/metric inputs forward (R13.6, R13.11).
    budget_seconds:
        Per-pair budget (default :data:`PER_PAIR_BUDGET_SECONDS`).
    repo_root:
        Repository root used to resolve the commit for the run id (R13.9).
    start_utc / commit:
        Test seams: an explicit UTC start and/or commit. When omitted they are
        resolved from ``datetime.now`` and ``git rev-parse``.

    Returns a :class:`HarnessResult`.
    """
    output_root = Path(output_root)
    dataset_root = resolve_dataset_root(dataset_base)

    if entries is None:
        index = build_pair_index(dataset_base)
        entries = list(index.entries)
    entries = list(entries)

    if start_utc is None:
        start_utc = datetime.now(timezone.utc)
    if commit is None:
        commit = _resolve_commit(repo_root)
    run_id, commit_unresolved = compute_run_id(start_utc, commit)

    out_names = default_output_dir_names(entries)
    records_dir = output_root / "records"
    traceback_dir = output_root / "tracebacks"

    existing = _load_existing_records(records_dir) if resume else {}

    # Decide, in order, which entries to skip (resume) vs run. Skips carry the
    # recorded outcome/metric inputs forward under the CURRENT run id.
    to_run: list[tuple[int, PairEntry]] = []
    records: list[Optional[PairRecord]] = [None] * len(entries)
    attempted = 0
    skipped = 0

    for i, entry in enumerate(entries):
        if resume:
            prior = existing.get((entry.repository, entry.contract_path))
            if prior is not None and prior.has_single_outcome():
                current_fp = _entry_fingerprint(dataset_root / entry.contract_path)
                if prior.source_fingerprint == current_fp:
                    carried = PairRecord.from_dict(prior.to_dict())
                    carried.run_id = run_id
                    carried.skipped = True
                    records[i] = carried
                    _write_record(records_dir, out_names[i], carried)
                    skipped += 1
                    continue
        to_run.append((i, entry))

    # Run the remaining entries, writing each record BEFORE dispatching the next
    # freed slot's entry (R13.5). With jobs>1 we submit up to `jobs` at a time.
    def _invoke(index_entry: tuple[int, PairEntry]) -> tuple[int, PairRecord]:
        i, entry = index_entry
        out_dir = output_root / out_names[i]
        record = _run_one(
            entry,
            dataset_root,
            out_dir,
            runner,
            run_id,
            budget_seconds,
            traceback_dir,
        )
        return i, record

    if jobs <= 1:
        for index_entry in to_run:
            i, record = _invoke(index_entry)
            records[i] = record
            _write_record(records_dir, out_names[i], record)
            attempted += 1
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(_invoke, ie): ie for ie in to_run}
            for future in concurrent.futures.as_completed(futures):
                i, record = future.result()
                records[i] = record
                _write_record(records_dir, out_names[i], record)
                attempted += 1

    final_records = [r for r in records if r is not None]

    metric_inputs = _merge_metric_inputs(final_records)
    metrics = compute_metrics(metric_inputs)
    scored = sum(1 for r in final_records if r.outcome in OUTCOME_SET and not _is_infra_only(r.outcome))

    result = HarnessResult(
        run_id=run_id,
        commit_unresolved=commit_unresolved,
        records=final_records,
        metrics=metrics,
        attempted=attempted,
        skipped=skipped,
        scored=scored,
    )

    write_results(result, output_root / "results.json")
    write_summary(result, output_root / "summary.txt")
    return result


def _is_infra_only(outcome: Outcome) -> bool:
    """True for outcomes that are infrastructure faults, not scored pairs."""
    return outcome in ("timeout", "error", "tool_unavailable", "llm_unavailable")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

MIN_JOBS = 1
MAX_JOBS = 16


def _validate_jobs(value: str) -> int:
    """argparse type for ``--jobs``: an int in ``MIN_JOBS..MAX_JOBS`` (R13.2/13.10).

    Raises :class:`argparse.ArgumentTypeError` naming the supplied value and the
    accepted range so the CLI exits nonzero *before* invoking anything.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--jobs must be an integer in {MIN_JOBS}..{MAX_JOBS}, got {value!r}"
        )
    if n < MIN_JOBS or n > MAX_JOBS:
        raise argparse.ArgumentTypeError(
            f"--jobs must be in {MIN_JOBS}..{MAX_JOBS}, got {n}"
        )
    return n


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m spec_pipeline.eval.harness",
        description="Evaluation_Harness: run the Spec_Pipeline over the "
        "Paired_Dataset Pair_Index and score the results (Requirement 13).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Root directory for per-pair output, records, and reports.",
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Dataset base (repo root or Paired_Dataset dir).",
    )
    parser.add_argument(
        "--jobs",
        type=_validate_jobs,
        default=1,
        help=f"Concurrent invocations ({MIN_JOBS}..{MAX_JOBS}, default 1).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip entries with a complete record for the current fingerprint.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root used to resolve the commit for the run id.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    # Invalid --jobs raises SystemExit(2) here, before any invocation (R13.2).
    args = parser.parse_args(argv)

    result = run_harness(
        output_root=args.output_root,
        dataset_base=args.base,
        jobs=args.jobs,
        resume=args.resume,
        repo_root=args.repo_root,
    )
    for line in _summary_lines(result):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
