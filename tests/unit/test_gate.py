"""Unit tests for the Quality_Gate (Requirement 14).

The module under test, ``spec_pipeline.eval.gate``, is import-safe without
slither (pure comparison over MetricReport data / plain dicts). To avoid
triggering ``spec_pipeline/__init__.py``'s eager slither-backed imports, we load
the module by file path via importlib (mirroring tests/unit/test_metrics.py) and
fall back to a plain import.

Coverage:
* R14.1 per-metric {floor, tolerance, direction} config, default tolerance 0.00
* R14.2 exit 0 when every floor and tolerance check passes
* R14.3 floor breach (at-least below / at-most above) -> exit 1
* R14.4 regression beyond tolerance vs baseline -> exit 1
* R14.5 no baseline -> record observed values, report (not fail) floor
        violations, mark provisional floors pending, exit 0
* R14.6 --update-baseline overwrites only when all checks pass; leaves
        null-metric baseline values unchanged
* R14.7 provisional floors seeded in the config file
* R14.8 rules_per_gated_function observed value is the >=4 aggregate
* R14.9 null computed metric excluded from checks, reported with reason, never 0
* R14.10 absent/unparseable/invalid config -> exit 2 naming the entry
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / "spec_pipeline" / "eval" / "gate_config.json"


def _load_module(name: str, rel: str):
    path = _REPO_ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


try:  # prefer the real package path; fall back to direct-file load
    from spec_pipeline.eval import gate as g  # type: ignore
    from spec_pipeline.eval import metrics as m  # type: ignore
except Exception:  # pragma: no cover - slither absent
    m = _load_module("gate_metrics_uut", "spec_pipeline/eval/metrics.py")
    g = _load_module("gate_uut", "spec_pipeline/eval/gate.py")


# ---------------------------------------------------------------------------
# Report / metric builders
# ---------------------------------------------------------------------------


def _metric(name: str, value):
    """A computed metric dict, or a null metric when value is None."""
    if value is None:
        return {"name": name, "numerator": 0, "denominator": 0,
                "value": None, "reason": "not_run"}
    return {"name": name, "numerator": 1, "denominator": 1, "value": str(value),
            "reason": None}


def _report(
    *,
    syntax=0.95,
    verdict=0.80,
    vacuity=0.10,
    effective=0.70,
    coverage=0.50,
    rules_agg=0.40,
) -> dict:
    """A MetricReport-shaped dict. Pass None for any metric to make it null."""
    return {
        "syntax_validity_rate": _metric("syntax_validity_rate", syntax),
        "verdict_rate": _metric("verdict_rate", verdict),
        "vacuity_rate": _metric("vacuity_rate", vacuity),
        "effective_pass_rate": _metric("effective_pass_rate", effective),
        "ground_truth_coverage": _metric("ground_truth_coverage", coverage),
        "rules_per_gated_function": [],
        "rules_per_gated_function_aggregate": _metric(
            "rules_per_gated_function_aggregate", rules_agg
        ),
        "excluded_counts": {},
    }


def _run(tmp_path, report, *, baseline=None, update=False, config=None,
         run_id="run-1"):
    baseline_path = tmp_path / "baseline.json"
    if baseline is not None:
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    return g.run_gate(
        report,
        config_path=config or _CONFIG,
        baseline_path=baseline_path,
        run_id=run_id,
        update_baseline=update,
    ), baseline_path


# ---------------------------------------------------------------------------
# R14.1 / R14.7 — config
# ---------------------------------------------------------------------------


def test_config_seeds_provisional_floors():
    config = g.load_gate_config(_CONFIG)
    assert config["syntax_validity_rate"].floor == Decimal("0.9")
    assert config["syntax_validity_rate"].direction == g.AT_LEAST
    assert config["verdict_rate"].floor == Decimal("0.75")
    assert config["vacuity_rate"].floor == Decimal("0.2")
    assert config["vacuity_rate"].direction == g.AT_MOST
    assert config["effective_pass_rate"].floor == Decimal("0.6")
    assert config["ground_truth_coverage"].floor == Decimal("0.4")
    assert config["rules_per_gated_function"].floor == Decimal("0.5")
    assert config["rules_per_gated_function"].direction == g.AT_MOST
    # Default tolerance 0.00 (R14.1).
    for rule in config.values():
        assert rule.tolerance == Decimal("0.0")


# ---------------------------------------------------------------------------
# R14.2 — all pass -> exit 0
# ---------------------------------------------------------------------------


def test_all_checks_pass_exit_0_against_baseline(tmp_path):
    # A baseline exists and observed meets floors and holds against baseline.
    baseline = {"run_id": "base", "metrics": {
        "syntax_validity_rate": "0.95",
        "verdict_rate": "0.80",
        "vacuity_rate": "0.10",
        "effective_pass_rate": "0.70",
        "ground_truth_coverage": "0.50",
        "rules_per_gated_function": "0.40",
    }}
    outcome, _ = _run(tmp_path, _report(), baseline=baseline)
    assert outcome.exit_code == 0
    assert not outcome.bootstrapped


def test_equal_after_rounding_treated_as_equal(tmp_path):
    # Observed exactly at the floor passes an at-least check (R14.2 rounding).
    baseline = {"run_id": "base", "metrics": {
        "syntax_validity_rate": "0.9000",
    }}
    outcome, _ = _run(tmp_path, _report(syntax=0.90), baseline=baseline)
    syntax = next(c for c in outcome.checks if c.name == "syntax_validity_rate")
    assert syntax.floor_ok


# ---------------------------------------------------------------------------
# R14.3 — floor breach both directions -> exit 1
# ---------------------------------------------------------------------------


def test_floor_breach_at_least_exit_1(tmp_path):
    # syntax_validity_rate below its 0.90 floor.
    outcome, _ = _run(tmp_path, _report(syntax=0.85),
                      baseline={"run_id": "b", "metrics": {}})
    assert outcome.exit_code == 1
    syntax = next(c for c in outcome.checks if c.name == "syntax_validity_rate")
    assert not syntax.floor_ok
    assert syntax.direction == g.AT_LEAST
    assert any("FLOOR BREACH" in ln and "syntax_validity_rate" in ln
               for ln in outcome.lines)


def test_floor_breach_at_most_exit_1(tmp_path):
    # vacuity_rate above its 0.20 floor (at-most: higher is worse).
    outcome, _ = _run(tmp_path, _report(vacuity=0.35),
                      baseline={"run_id": "b", "metrics": {}})
    assert outcome.exit_code == 1
    vac = next(c for c in outcome.checks if c.name == "vacuity_rate")
    assert not vac.floor_ok
    assert vac.direction == g.AT_MOST


def test_floor_breach_reports_metric_observed_floor_direction(tmp_path):
    outcome, _ = _run(tmp_path, _report(syntax=0.85),
                      baseline={"run_id": "b", "metrics": {}})
    line = next(ln for ln in outcome.lines if "syntax_validity_rate" in ln)
    assert "0.85" in line
    assert "0.9" in line  # floor
    assert g.AT_LEAST in line


# ---------------------------------------------------------------------------
# R14.4 — regression beyond tolerance -> exit 1
# ---------------------------------------------------------------------------


def test_regression_at_least_beyond_tolerance_exit_1(tmp_path):
    # coverage dropped from 0.50 baseline to 0.45; still above its 0.40 floor,
    # but tolerance is 0.00 so any drop is a regression.
    baseline = {"run_id": "base", "metrics": {"ground_truth_coverage": "0.50"}}
    outcome, _ = _run(tmp_path, _report(coverage=0.45), baseline=baseline)
    assert outcome.exit_code == 1
    cov = next(c for c in outcome.checks if c.name == "ground_truth_coverage")
    assert cov.floor_ok  # still above floor
    assert not cov.regression_ok
    assert any("REGRESSION" in ln and "ground_truth_coverage" in ln
               for ln in outcome.lines)


def test_regression_at_most_beyond_tolerance_exit_1(tmp_path):
    # vacuity rose from 0.10 baseline to 0.15; still below its 0.20 floor, but
    # an at-most metric rising is a regression at tolerance 0.
    baseline = {"run_id": "base", "metrics": {"vacuity_rate": "0.10"}}
    outcome, _ = _run(tmp_path, _report(vacuity=0.15), baseline=baseline)
    assert outcome.exit_code == 1
    vac = next(c for c in outcome.checks if c.name == "vacuity_rate")
    assert vac.floor_ok
    assert not vac.regression_ok


def test_within_tolerance_not_a_regression(tmp_path):
    # A custom config with a 0.05 tolerance tolerates a 0.03 drop.
    cfg = tmp_path / "cfg.json"
    base_cfg = json.loads(_CONFIG.read_text())
    base_cfg["ground_truth_coverage"]["tolerance"] = 0.05
    cfg.write_text(json.dumps(base_cfg), encoding="utf-8")
    baseline = {"run_id": "base", "metrics": {"ground_truth_coverage": "0.50"}}
    outcome, _ = _run(tmp_path, _report(coverage=0.47), baseline=baseline,
                      config=cfg)
    cov = next(c for c in outcome.checks if c.name == "ground_truth_coverage")
    assert cov.regression_ok
    assert outcome.exit_code == 0


# ---------------------------------------------------------------------------
# R14.5 — no baseline bootstrap -> exit 0 + baseline recorded
# ---------------------------------------------------------------------------


def test_no_baseline_bootstrap_records_and_exit_0(tmp_path):
    outcome, baseline_path = _run(tmp_path, _report(), run_id="run-42")
    assert outcome.exit_code == 0
    assert outcome.bootstrapped
    assert outcome.baseline_written
    assert baseline_path.exists()
    stored = json.loads(baseline_path.read_text())
    assert stored["run_id"] == "run-42"
    assert stored["metrics"]["syntax_validity_rate"] == "0.9500"


def test_no_baseline_reports_floor_violations_without_failing(tmp_path):
    # Even with a floor breach, bootstrap exits 0 and marks it pending.
    outcome, _ = _run(tmp_path, _report(syntax=0.10), run_id="run-1")
    assert outcome.exit_code == 0
    assert outcome.bootstrapped
    assert any("provisional floor pending" in ln and "syntax_validity_rate" in ln
               for ln in outcome.lines)


# ---------------------------------------------------------------------------
# R14.6 — --update-baseline gating
# ---------------------------------------------------------------------------


def test_update_baseline_writes_when_all_pass(tmp_path):
    baseline = {"run_id": "old", "metrics": {
        "syntax_validity_rate": "0.90",
        "verdict_rate": "0.75",
        "vacuity_rate": "0.20",
        "effective_pass_rate": "0.60",
        "ground_truth_coverage": "0.40",
        "rules_per_gated_function": "0.50",
    }}
    # Observed strictly better or equal everywhere -> all pass.
    report = _report(syntax=0.95, verdict=0.80, vacuity=0.10, effective=0.70,
                     coverage=0.50, rules_agg=0.40)
    outcome, baseline_path = _run(tmp_path, report, baseline=baseline,
                                  update=True, run_id="run-new")
    assert outcome.exit_code == 0
    assert outcome.baseline_written
    stored = json.loads(baseline_path.read_text())
    assert stored["run_id"] == "run-new"
    assert stored["metrics"]["syntax_validity_rate"] == "0.9500"


def test_update_baseline_not_written_when_a_check_fails(tmp_path):
    baseline = {"run_id": "old", "metrics": {"syntax_validity_rate": "0.95"}}
    # Floor breach -> update must not write.
    outcome, baseline_path = _run(tmp_path, _report(syntax=0.10),
                                  baseline=baseline, update=True)
    assert outcome.exit_code == 1
    assert not outcome.baseline_written
    stored = json.loads(baseline_path.read_text())
    assert stored["run_id"] == "old"  # unchanged


def test_update_baseline_leaves_null_metric_values_unchanged(tmp_path):
    baseline = {"run_id": "old", "metrics": {
        "syntax_validity_rate": "0.95",
        "verdict_rate": "0.80",
        "vacuity_rate": "0.10",
        "effective_pass_rate": "0.70",
        "ground_truth_coverage": "0.50",
        "rules_per_gated_function": "0.40",
    }}
    # coverage becomes null; all other checks pass -> baseline written, but the
    # coverage value must retain its previous non-null baseline value (R14.6).
    report = _report(coverage=None)
    outcome, baseline_path = _run(tmp_path, report, baseline=baseline,
                                  update=True, run_id="run-new")
    assert outcome.exit_code == 0
    assert outcome.baseline_written
    stored = json.loads(baseline_path.read_text())
    assert stored["metrics"]["ground_truth_coverage"] == "0.50"


# ---------------------------------------------------------------------------
# R14.8 — rules_per_gated_function uses the >=4 aggregate
# ---------------------------------------------------------------------------


def test_rules_per_gated_function_uses_aggregate_at_most(tmp_path):
    # aggregate above the 0.50 floor -> at-most breach.
    outcome, _ = _run(tmp_path, _report(rules_agg=0.75),
                      baseline={"run_id": "b", "metrics": {}})
    rpgf = next(c for c in outcome.checks
                if c.name == "rules_per_gated_function")
    assert rpgf.observed == Decimal("0.7500")
    assert not rpgf.floor_ok
    assert rpgf.direction == g.AT_MOST
    assert outcome.exit_code == 1


def test_rules_per_gated_function_aggregate_null_excluded(tmp_path):
    # When no cohort has >=4 gated fns the aggregate is null -> excluded.
    report = _report(rules_agg=None)
    outcome, _ = _run(tmp_path, report, baseline={"run_id": "b", "metrics": {}})
    rpgf = next(c for c in outcome.checks
                if c.name == "rules_per_gated_function")
    assert rpgf.is_null
    assert outcome.exit_code == 0


# ---------------------------------------------------------------------------
# R14.9 — null computed metric excluded, reported, never 0
# ---------------------------------------------------------------------------


def test_null_metric_excluded_and_reported(tmp_path):
    report = _report(syntax=None)
    outcome, _ = _run(tmp_path, report, baseline={"run_id": "b", "metrics": {}})
    syntax = next(c for c in outcome.checks if c.name == "syntax_validity_rate")
    assert syntax.is_null
    assert syntax.observed is None  # never substituted with 0
    assert syntax.floor_ok and syntax.regression_ok
    assert syntax.null_reason == "not_run"
    assert any("null" in ln and "syntax_validity_rate" in ln
               for ln in outcome.lines)
    # A null metric does not by itself cause a failure.
    assert outcome.exit_code == 0


def test_null_metric_not_treated_as_zero_for_at_least(tmp_path):
    # If null were treated as 0, an at-least floor of 0.90 would breach. It must
    # not: the metric is excluded, so the gate passes.
    report = _report(effective=None)
    outcome, _ = _run(tmp_path, report, baseline={"run_id": "b", "metrics": {}})
    assert outcome.exit_code == 0


# ---------------------------------------------------------------------------
# R14.10 — invalid/absent config -> exit 2, baseline unchanged
# ---------------------------------------------------------------------------


def test_absent_config_exit_2_baseline_unchanged(tmp_path):
    baseline = {"run_id": "old", "metrics": {"syntax_validity_rate": "0.95"}}
    missing = tmp_path / "does_not_exist.json"
    outcome, baseline_path = _run(tmp_path, _report(), baseline=baseline,
                                  config=missing)
    assert outcome.exit_code == 2
    stored = json.loads(baseline_path.read_text())
    assert stored["run_id"] == "old"


def test_unparseable_config_exit_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    outcome, _ = _run(tmp_path, _report(), baseline={"run_id": "b",
                      "metrics": {}}, config=bad)
    assert outcome.exit_code == 2


def test_config_missing_key_exit_2_names_entry(tmp_path):
    cfg = json.loads(_CONFIG.read_text())
    del cfg["vacuity_rate"]["tolerance"]
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    outcome, _ = _run(tmp_path, _report(), baseline={"run_id": "b",
                      "metrics": {}}, config=p)
    assert outcome.exit_code == 2
    assert any("vacuity_rate" in ln for ln in outcome.lines)


def test_config_floor_out_of_range_exit_2(tmp_path):
    cfg = json.loads(_CONFIG.read_text())
    cfg["verdict_rate"]["floor"] = 1.5
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    outcome, _ = _run(tmp_path, _report(), baseline={"run_id": "b",
                      "metrics": {}}, config=p)
    assert outcome.exit_code == 2
    assert any("verdict_rate" in ln for ln in outcome.lines)


def test_config_negative_tolerance_exit_2(tmp_path):
    cfg = json.loads(_CONFIG.read_text())
    cfg["effective_pass_rate"]["tolerance"] = -0.01
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    outcome, _ = _run(tmp_path, _report(), baseline={"run_id": "b",
                      "metrics": {}}, config=p)
    assert outcome.exit_code == 2
    assert any("effective_pass_rate" in ln for ln in outcome.lines)


# ---------------------------------------------------------------------------
# Integration with the real MetricReport type (R14.2 via metrics module)
# ---------------------------------------------------------------------------


def test_run_gate_accepts_real_metric_report_object(tmp_path):
    report = m.compute_metrics(
        m.EvaluationInputs(
            spec_typechecks=(m.SpecTypecheck("p1", "accepted"),),
        )
    )
    # Only syntax_validity_rate is non-null; everything else is null and thus
    # excluded. Bootstrap path -> exit 0.
    baseline_path = tmp_path / "baseline.json"
    outcome = g.run_gate(
        report,
        config_path=_CONFIG,
        baseline_path=baseline_path,
        run_id="obj-run",
    )
    assert outcome.exit_code == 0
    assert outcome.bootstrapped
    stored = json.loads(baseline_path.read_text())
    assert stored["metrics"]["syntax_validity_rate"] == "1.0000"
    # Null metrics recorded as null, never 0.
    assert stored["metrics"]["verdict_rate"] is None
