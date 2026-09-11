"""Quality_Gate — compare computed metrics against floors and the baseline (R14).

This module is the single owner of the evaluation quality gate. It reads a
version-controlled gate configuration file holding a per-metric
``{floor, tolerance, direction}`` triple (seeded from the provisional floors in
Requirement 14.7), compares each non-null computed metric against its floor and
against the stored :class:`Evaluation_Baseline`, and selects an exit code.

Design constraints anchored to the requirements:

* **Import-safe without slither.** This module operates only on the plain
  :class:`~spec_pipeline.eval.metrics.MetricReport` data (and plain dicts). It
  may import ``spec_pipeline.eval.metrics`` — which is itself slither-free — but
  imports nothing slither-backed.
* **Directions differ.** ``vacuity_rate`` and ``rules_per_gated_function`` (the
  aggregate over cohorts of 4 or more gated functions, R12.12/R14.8) are
  ``"at most"`` (lower is better); every other metric is ``"at least"``.
* **Nulls are never zero (R14.9, R12).** A null computed metric is excluded
  from floor and tolerance checks, reported with its reason code, and never
  substituted with 0.
* **Rounding.** All comparisons treat two values as equal when they agree after
  rounding half-up to 4 decimal places (matching ``metrics.round_half_up_4``).

Exit codes (returned by :func:`run_gate`):

* ``0`` — every floor and tolerance check passed, OR no baseline existed and the
  observed values were recorded as the bootstrap baseline (R14.2, R14.5).
* ``1`` — a floor breach (at-least below floor / at-most above floor) or a
  regression beyond tolerance versus the baseline (R14.3, R14.4).
* ``2`` — the config was absent, unparseable, or invalid (missing key, floor
  outside 0..1, negative tolerance); the baseline is left unchanged (R14.10).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

try:  # prefer the package path; fall back to a direct-file load (slither-free)
    from spec_pipeline.eval.metrics import MetricReport  # type: ignore
except Exception:  # pragma: no cover - exercised only when the package is broken
    MetricReport = object  # type: ignore


# ---------------------------------------------------------------------------
# Metric name / direction constants
# ---------------------------------------------------------------------------

AT_LEAST = "at least"
AT_MOST = "at most"

# The observed value for ``rules_per_gated_function`` is the aggregate over
# cohorts of 4 or more gated functions (R14.8, R12.12).
RULES_PER_GATED_FUNCTION = "rules_per_gated_function"

# The six gated metrics and the direction each is measured in (R14.7).
METRIC_DIRECTIONS = {
    "syntax_validity_rate": AT_LEAST,
    "verdict_rate": AT_LEAST,
    "vacuity_rate": AT_MOST,
    "effective_pass_rate": AT_LEAST,
    "ground_truth_coverage": AT_LEAST,
    RULES_PER_GATED_FUNCTION: AT_MOST,
}


class GateConfigError(Exception):
    """Raised when the gate configuration is absent, unparseable, or invalid.

    Carries the offending entry name so the caller can name it in the exit-2
    report (R14.10).
    """


# ---------------------------------------------------------------------------
# Rounding / comparison helpers
# ---------------------------------------------------------------------------


def _q4(value) -> Decimal:
    """Round ``value`` half-up to 4 decimal places as a :class:`Decimal`.

    Accepts ``Decimal``, ``float``, ``int``, or numeric ``str``. Two values are
    treated as equal by the gate when their ``_q4`` forms are equal.
    """
    if isinstance(value, Decimal):
        dec = value
    else:
        dec = Decimal(str(value))
    return dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Gate configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricFloor:
    """One metric's gate rule: floor, regression tolerance, and direction."""

    floor: Decimal
    tolerance: Decimal
    direction: str


def _validate_entry(name: str, raw: object) -> MetricFloor:
    """Validate one config entry, raising :class:`GateConfigError` naming it."""
    if not isinstance(raw, dict):
        raise GateConfigError(f"{name}: entry must be an object")
    for key in ("floor", "tolerance", "direction"):
        if key not in raw:
            raise GateConfigError(f"{name}: missing '{key}'")
    direction = raw["direction"]
    if direction not in (AT_LEAST, AT_MOST):
        raise GateConfigError(
            f"{name}: direction must be '{AT_LEAST}' or '{AT_MOST}', "
            f"got {direction!r}"
        )
    try:
        floor = Decimal(str(raw["floor"]))
        tolerance = Decimal(str(raw["tolerance"]))
    except Exception as exc:  # non-numeric floor/tolerance
        raise GateConfigError(f"{name}: floor/tolerance must be numeric") from exc
    if floor < Decimal("0") or floor > Decimal("1"):
        raise GateConfigError(f"{name}: floor {floor} outside 0..1")
    if tolerance < Decimal("0"):
        raise GateConfigError(f"{name}: tolerance {tolerance} is negative")
    return MetricFloor(floor=floor, tolerance=tolerance, direction=direction)


def load_gate_config(path: Path) -> dict[str, MetricFloor]:
    """Load and validate the version-controlled gate config file (R14.1, R14.10).

    Raises :class:`GateConfigError` naming the offending entry when the file is
    absent, unparseable, or holds an invalid entry (missing floor/tolerance/
    direction, floor outside 0..1, or negative tolerance).
    """
    if not path.exists():
        raise GateConfigError(f"{path}: gate config file not found")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise GateConfigError(f"{path}: gate config is unparseable ({exc})") from exc
    if not isinstance(raw, dict):
        raise GateConfigError(f"{path}: gate config must be a JSON object")

    config: dict[str, MetricFloor] = {}
    for name, entry in raw.items():
        config[name] = _validate_entry(name, entry)
    # Every gated metric named in R14.7 must be present.
    for required in METRIC_DIRECTIONS:
        if required not in config:
            raise GateConfigError(f"{required}: missing from gate config")
    return config


# ---------------------------------------------------------------------------
# Observed metric extraction (null-aware, R14.9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedMetric:
    """One metric's observed value for the gate.

    ``value`` is the rounded Decimal when the metric was computed, or ``None``
    when the metric is null (excluded from floor/tolerance checks, R14.9).
    ``reason`` carries the Outcome_Set reason code for a null metric.
    """

    name: str
    value: Optional[Decimal]
    reason: Optional[str] = None

    @property
    def is_null(self) -> bool:
        return self.value is None


def _observed_from_metric(name: str, metric) -> ObservedMetric:
    """Build an :class:`ObservedMetric` from a ``Metric``-like object.

    Accepts either a :class:`~spec_pipeline.eval.metrics.Metric` (with ``value``
    / ``reason`` attributes) or a plain dict with ``value`` / ``reason`` keys.
    """
    if isinstance(metric, dict):
        value = metric.get("value")
        reason = metric.get("reason")
    else:
        value = getattr(metric, "value", None)
        reason = getattr(metric, "reason", None)
    if value is None:
        return ObservedMetric(name, None, reason)
    return ObservedMetric(name, _q4(value), None)


def observed_metrics(report) -> dict[str, ObservedMetric]:
    """Extract the six gated observed metrics from a MetricReport-like object.

    For ``rules_per_gated_function`` the observed value is the aggregate over
    cohorts of 4 or more gated functions (R14.8): the report's
    ``rules_per_gated_function_aggregate`` metric.

    Accepts a :class:`~spec_pipeline.eval.metrics.MetricReport` instance or a
    plain dict shaped like ``MetricReport.as_dict()``.
    """
    if isinstance(report, dict):
        get = report.get

        def metric_for(key: str):
            return get(key)

        agg = get("rules_per_gated_function_aggregate")
    else:
        def metric_for(key: str):
            return getattr(report, key, None)

        agg = getattr(report, "rules_per_gated_function_aggregate", None)

    out: dict[str, ObservedMetric] = {}
    for name in METRIC_DIRECTIONS:
        if name == RULES_PER_GATED_FUNCTION:
            source = agg
        else:
            source = metric_for(name)
        if source is None:
            # No metric emitted at all -> treat as null with a not_run reason.
            out[name] = ObservedMetric(name, None, "not_run")
        else:
            out[name] = _observed_from_metric(name, source)
    return out


# ---------------------------------------------------------------------------
# Baseline persistence (R14.5, R14.6)
# ---------------------------------------------------------------------------


def load_baseline(path: Path) -> Optional[dict]:
    """Load the Evaluation_Baseline, or None when it does not exist (R14.5)."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_payload(run_id: str, observed: dict[str, ObservedMetric]) -> dict:
    """Build a baseline record: run id + per-metric value (null stays null)."""
    metrics: dict[str, Optional[str]] = {}
    for name, obs in observed.items():
        metrics[name] = None if obs.is_null else str(obs.value)
    return {"run_id": run_id, "metrics": metrics}


def write_baseline(path: Path, payload: dict) -> None:
    """Write the baseline as canonical JSON (sorted keys, 2-space, trailing LF)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def _baseline_value(baseline: Optional[dict], name: str) -> Optional[Decimal]:
    """Return the baseline value for ``name`` as a Decimal, or None.

    None means the baseline holds no non-null value for the metric (either the
    baseline is absent, the metric is absent, or its recorded value is null).
    Null baseline values are never treated as zero.
    """
    if not baseline:
        return None
    metrics = baseline.get("metrics", {})
    raw = metrics.get(name)
    if raw is None:
        return None
    return _q4(raw)


# ---------------------------------------------------------------------------
# Check evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """The outcome of gating one metric."""

    name: str
    direction: str
    observed: Optional[Decimal]
    floor: Decimal
    tolerance: Decimal
    baseline: Optional[Decimal]
    null_reason: Optional[str]
    floor_ok: bool
    regression_ok: bool

    @property
    def is_null(self) -> bool:
        return self.observed is None

    @property
    def passed(self) -> bool:
        return self.floor_ok and self.regression_ok


def _floor_ok(direction: str, observed: Decimal, floor: Decimal) -> bool:
    """A floor holds when at-least >= floor, or at-most <= floor (R14.3)."""
    if direction == AT_LEAST:
        return observed >= floor
    return observed <= floor


def _regression_ok(
    direction: str,
    observed: Decimal,
    baseline: Optional[Decimal],
    tolerance: Decimal,
) -> bool:
    """No regression beyond tolerance versus the baseline (R14.4).

    For an at-least metric, a regression is observed dropping below
    ``baseline - tolerance``. For an at-most metric (lower is better), a
    regression is observed rising above ``baseline + tolerance``.
    """
    if baseline is None:
        return True
    if direction == AT_LEAST:
        return observed >= baseline - tolerance
    return observed <= baseline + tolerance


def evaluate_checks(
    observed: dict[str, ObservedMetric],
    config: dict[str, MetricFloor],
    baseline: Optional[dict],
) -> list[CheckResult]:
    """Evaluate every gated metric against its floor and the baseline.

    Null metrics are excluded from floor and tolerance checks (they pass both,
    carrying their reason for reporting) and are never substituted with 0
    (R14.9).
    """
    results: list[CheckResult] = []
    for name in sorted(METRIC_DIRECTIONS):
        rule = config[name]
        obs = observed[name]
        base = _baseline_value(baseline, name)
        if obs.is_null:
            results.append(
                CheckResult(
                    name=name,
                    direction=rule.direction,
                    observed=None,
                    floor=rule.floor,
                    tolerance=rule.tolerance,
                    baseline=base,
                    null_reason=obs.reason,
                    floor_ok=True,
                    regression_ok=True,
                )
            )
            continue
        floor_ok = _floor_ok(rule.direction, obs.value, rule.floor)
        regression_ok = _regression_ok(
            rule.direction, obs.value, base, rule.tolerance
        )
        results.append(
            CheckResult(
                name=name,
                direction=rule.direction,
                observed=obs.value,
                floor=rule.floor,
                tolerance=rule.tolerance,
                baseline=base,
                null_reason=None,
                floor_ok=floor_ok,
                regression_ok=regression_ok,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Top-level gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateOutcome:
    """The full result of running the gate."""

    exit_code: int
    checks: list[CheckResult]
    lines: list[str]
    baseline_written: bool
    bootstrapped: bool


def _format_line(check: CheckResult) -> str:
    """One human-readable report line for a metric (R14.3, R14.4, R14.9)."""
    if check.is_null:
        return (
            f"{check.name}: null (reason={check.null_reason}) — "
            f"excluded from floor and tolerance checks"
        )
    parts = [f"{check.name}: observed={check.observed} ({check.direction})"]
    if not check.floor_ok:
        parts.append(f"FLOOR BREACH floor={check.floor}")
    if not check.regression_ok:
        parts.append(
            f"REGRESSION baseline={check.baseline} tolerance={check.tolerance}"
        )
    if check.floor_ok and check.regression_ok:
        parts.append(f"ok floor={check.floor}")
        if check.baseline is not None:
            parts.append(f"baseline={check.baseline}")
    return " ".join(parts)


def run_gate(
    report,
    *,
    config_path: Path,
    baseline_path: Path,
    run_id: str,
    update_baseline: bool = False,
) -> GateOutcome:
    """Run the Quality_Gate for one evaluation run (R14.2-R14.6, R14.9, R14.10).

    Parameters
    ----------
    report:
        A :class:`~spec_pipeline.eval.metrics.MetricReport` or a plain dict
        shaped like ``MetricReport.as_dict()``.
    config_path:
        Path to the version-controlled gate config file.
    baseline_path:
        Path to the Evaluation_Baseline file (may be absent).
    run_id:
        The evaluation run identifier recorded alongside a bootstrapped or
        updated baseline.
    update_baseline:
        When True, overwrite the baseline only if every floor and tolerance
        check passes, leaving null-metric baseline values unchanged (R14.6).

    Returns a :class:`GateOutcome`. On a config error the outcome carries exit
    code 2 and the baseline is left unchanged (R14.10).
    """
    try:
        config = load_gate_config(config_path)
    except GateConfigError as exc:
        return GateOutcome(
            exit_code=2,
            checks=[],
            lines=[f"invalid gate config: {exc}"],
            baseline_written=False,
            bootstrapped=False,
        )

    observed = observed_metrics(report)
    baseline = load_baseline(baseline_path)
    checks = evaluate_checks(observed, config, baseline)
    lines = [_format_line(c) for c in checks]

    # Bootstrap: no baseline yet (R14.5). Record observed non-null values, report
    # floor violations WITHOUT failing, mark provisional floors pending, exit 0.
    if baseline is None:
        write_baseline(baseline_path, _baseline_payload(run_id, observed))
        breaches = [c for c in checks if not c.is_null and not c.floor_ok]
        lines.append(
            "no baseline existed — recorded observed values as the "
            f"Evaluation_Baseline (run_id={run_id}); provisional floors pending "
            "review"
        )
        for c in breaches:
            lines.append(
                f"  provisional floor pending: {c.name} observed={c.observed} "
                f"floor={c.floor} ({c.direction})"
            )
        return GateOutcome(
            exit_code=0,
            checks=checks,
            lines=lines,
            baseline_written=True,
            bootstrapped=True,
        )

    all_passed = all(c.passed for c in checks)

    baseline_written = False
    if update_baseline:
        if all_passed:
            # Overwrite, but leave null-metric baseline values unchanged (R14.6).
            merged = _merge_baseline(baseline, observed, run_id)
            write_baseline(baseline_path, merged)
            baseline_written = True
            lines.append(f"--update-baseline: baseline overwritten (run_id={run_id})")
        else:
            lines.append(
                "--update-baseline: NOT written — one or more checks failed"
            )

    exit_code = 0 if all_passed else 1
    return GateOutcome(
        exit_code=exit_code,
        checks=checks,
        lines=lines,
        baseline_written=baseline_written,
        bootstrapped=False,
    )


def _merge_baseline(
    baseline: dict,
    observed: dict[str, ObservedMetric],
    run_id: str,
) -> dict:
    """Overwrite baseline values, leaving null-metric values unchanged (R14.6).

    A null observed metric does not clobber a previously recorded non-null
    baseline value (a null is never treated as a measurement).
    """
    existing = dict(baseline.get("metrics", {}))
    for name, obs in observed.items():
        if obs.is_null:
            # Leave the previously recorded value (if any) unchanged.
            continue
        existing[name] = str(obs.value)
    return {"run_id": run_id, "metrics": existing}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = Path(__file__).resolve().parent / "gate_config.json"


def _load_report_from_file(path: Path) -> dict:
    """Load a MetricReport-shaped dict from a JSON file (harness output)."""
    return json.loads(path.read_text(encoding="utf-8"))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m spec_pipeline.eval.gate",
        description="Quality_Gate: compare evaluation metrics against floors "
        "and the recorded baseline (Requirement 14).",
    )
    parser.add_argument(
        "metrics_file",
        type=Path,
        help="Path to a JSON file holding a MetricReport (as_dict shape).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help="Path to the version-controlled gate config file.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="Path to the Evaluation_Baseline file (created on bootstrap).",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Run identifier recorded with a bootstrapped/updated baseline.",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Overwrite the baseline only when every check passes.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        report = _load_report_from_file(args.metrics_file)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: could not read metrics file: {exc}", file=sys.stderr)
        return 12

    outcome = run_gate(
        report,
        config_path=args.config,
        baseline_path=args.baseline,
        run_id=args.run_id,
        update_baseline=args.update_baseline,
    )
    stream = sys.stderr if outcome.exit_code != 0 else sys.stdout
    for line in outcome.lines:
        print(line, file=stream)
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
