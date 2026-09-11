"""Unit tests for the Metric_Calculator (Requirement 12).

The module under test, ``spec_pipeline.eval.metrics``, is import-safe without
slither (pure computation over plain dicts/records). To avoid triggering
``spec_pipeline/__init__.py``'s eager slither-backed imports, we load the module
by file path via importlib (mirroring tests/unit/test_methods_block.py) and fall
back to a plain import.

Coverage:
* R12.1 syntax_validity_rate (accept / accept-or-reject; no_result excluded)
* R12.2 verdict_rate
* R12.3 vacuity_rate, R12.4 effective_pass_rate (shared denominator)
* R12.5 ground_truth_coverage (exact case-sensitive; parametric-all-methods)
* R12.6 rules_per_gated_function per cohort + R12.12 >=4 aggregate
* R12.7 per-pair telemetry (null-not-zero token counts, stage durations)
* R12.8 half-up rounding to 4 dp
* R12.9 null + Outcome_Set reason when input absent
* R12.10 null when denominator is zero
* R12.11 tool_unavailable / not_run exclusion + per-reason excluded counts
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_metrics():
    mod_name = "spec_pipeline_metrics_under_test"
    path = _REPO_ROOT / "spec_pipeline" / "eval" / "metrics.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


try:  # prefer the real package path; fall back to direct-file load
    from spec_pipeline.eval import metrics as m  # type: ignore
except Exception:  # pragma: no cover - slither absent
    m = _load_metrics()


# ---------------------------------------------------------------------------
# Rounding — R12.8
# ---------------------------------------------------------------------------


def test_round_half_up_4_ties_round_up():
    # 0.12345 -> 0.1235 (half up, not banker's rounding)
    assert m.round_half_up_4(12345, 100000) == Decimal("0.1235")


def test_round_half_up_4_exact_and_repeating():
    assert m.round_half_up_4(1, 2) == Decimal("0.5000")
    assert m.round_half_up_4(2, 3) == Decimal("0.6667")
    assert m.round_half_up_4(1, 1) == Decimal("1.0000")


# ---------------------------------------------------------------------------
# syntax_validity_rate — R12.1
# ---------------------------------------------------------------------------


def test_syntax_validity_rate_excludes_no_result():
    specs = (
        m.SpecTypecheck("a", "accepted"),
        m.SpecTypecheck("b", "accepted"),
        m.SpecTypecheck("c", "rejected"),
        m.SpecTypecheck("d", "no_result"),  # excluded from num and denom
    )
    metric = m.compute_syntax_validity_rate(specs)
    assert metric.numerator == 2
    assert metric.denominator == 3
    assert metric.value == Decimal("0.6667")
    assert metric.reason is None


def test_syntax_validity_rate_null_when_absent():
    metric = m.compute_syntax_validity_rate(())
    assert metric.is_null
    assert metric.value is None
    assert metric.reason == m.REASON_NO_INPUT


def test_syntax_validity_rate_null_when_all_no_result():
    specs = (m.SpecTypecheck("a", "no_result"),)
    metric = m.compute_syntax_validity_rate(specs)
    assert metric.is_null
    assert metric.denominator == 0
    assert metric.reason == m.REASON_EMPTY_DENOMINATOR


# ---------------------------------------------------------------------------
# verdict_rate — R12.2 + R12.11 exclusion
# ---------------------------------------------------------------------------


def _rep(pair_id, status="verified", verdicts=(), scored=True):
    return m.PairReport(
        pair_id=pair_id,
        scored=scored,
        report_status=status,
        verdicts=tuple(verdicts),
    )


def test_verdict_rate_basic():
    reports = (
        _rep("a", verdicts=[m.RuleVerdict("r1", "passing")]),
        _rep("b", verdicts=[]),  # no verdicts
    )
    metric = m.compute_verdict_rate(reports)
    assert metric.numerator == 1
    assert metric.denominator == 2
    assert metric.value == Decimal("0.5000")


def test_verdict_rate_excludes_tool_unavailable_and_not_run():
    reports = (
        _rep("a", verdicts=[m.RuleVerdict("r1", "passing")]),
        _rep("b", status="tool_unavailable"),
        _rep("c", status="not_run"),
        _rep("d", status=None),  # stage 5 never ran
    )
    metric = m.compute_verdict_rate(reports)
    # Only pair "a" is verdict-eligible.
    assert metric.numerator == 1
    assert metric.denominator == 1
    assert metric.value == Decimal("1.0000")


def test_verdict_rate_null_when_no_scored_pairs():
    metric = m.compute_verdict_rate(())
    assert metric.is_null
    assert metric.reason == m.REASON_NO_INPUT


# ---------------------------------------------------------------------------
# vacuity_rate + effective_pass_rate — R12.3 / R12.4 (shared denominator)
# ---------------------------------------------------------------------------


def test_vacuity_and_effective_pass_share_denominator():
    reports = (
        _rep(
            "a",
            verdicts=[
                m.RuleVerdict("r1", "passing"),
                m.RuleVerdict("r2", "vacuous"),
                m.RuleVerdict("r3", "failing"),
            ],
        ),
        _rep("b", verdicts=[m.RuleVerdict("r4", "passing")]),
    )
    vac = m.compute_vacuity_rate(reports)
    eff = m.compute_effective_pass_rate(reports)
    assert vac.denominator == 4
    assert eff.denominator == 4
    assert vac.numerator == 1  # one vacuous
    assert eff.numerator == 2  # two passing (non-vacuous)
    assert vac.value == Decimal("0.2500")
    assert eff.value == Decimal("0.5000")


def test_vacuity_rate_excludes_ineligible_pairs():
    reports = (
        _rep("a", verdicts=[m.RuleVerdict("r1", "vacuous")]),
        _rep("b", status="tool_unavailable", verdicts=[m.RuleVerdict("x", "passing")]),
    )
    vac = m.compute_vacuity_rate(reports)
    # tool_unavailable pair's verdicts are excluded from the denominator.
    assert vac.denominator == 1
    assert vac.numerator == 1


def test_vacuity_rate_null_when_no_rules_with_verdict():
    reports = (_rep("a", verdicts=[]),)
    vac = m.compute_vacuity_rate(reports)
    assert vac.is_null
    assert vac.denominator == 0
    assert vac.reason == m.REASON_EMPTY_DENOMINATOR


# ---------------------------------------------------------------------------
# ground_truth_coverage — R12.5
# ---------------------------------------------------------------------------


def test_ground_truth_coverage_exact_case_sensitive():
    gt = (
        m.GroundTruthProperty(
            contract="Token",
            name="p1",
            referenced_functions=frozenset({"transfer"}),
            referenced_state_vars=frozenset({"balanceOf"}),
        ),
    )
    rules = (
        m.GeneratedRule(
            contract="Token",
            name="g1",
            referenced_functions=frozenset({"transfer"}),
            referenced_state_vars=frozenset({"balanceOf"}),
        ),
    )
    metric = m.compute_ground_truth_coverage(gt, rules)
    assert metric.numerator == 1
    assert metric.denominator == 1
    assert metric.value == Decimal("1.0000")


def test_ground_truth_coverage_case_sensitivity_misses():
    gt = (
        m.GroundTruthProperty(
            contract="Token",
            name="p1",
            referenced_functions=frozenset({"transfer"}),
        ),
    )
    rules = (
        m.GeneratedRule(
            contract="Token",
            name="g1",
            referenced_functions=frozenset({"Transfer"}),  # wrong case
        ),
    )
    metric = m.compute_ground_truth_coverage(gt, rules)
    assert metric.numerator == 0
    assert metric.denominator == 1
    assert metric.value == Decimal("0.0000")


def test_ground_truth_coverage_requires_same_contract():
    gt = (
        m.GroundTruthProperty(
            contract="Token",
            name="p1",
            referenced_functions=frozenset({"transfer"}),
        ),
    )
    rules = (
        m.GeneratedRule(
            contract="Other",  # different contract
            name="g1",
            referenced_functions=frozenset({"transfer"}),
        ),
    )
    metric = m.compute_ground_truth_coverage(gt, rules)
    assert metric.numerator == 0


def test_ground_truth_coverage_parametric_covers_all_functions():
    gt = (
        m.GroundTruthProperty(
            contract="Token",
            name="p1",
            referenced_functions=frozenset({"transfer", "approve", "mint"}),
        ),
    )
    rules = (
        m.GeneratedRule(
            contract="Token",
            name="param",
            quantifies_all_methods=True,
        ),
    )
    metric = m.compute_ground_truth_coverage(gt, rules)
    # Parametric-all-methods covers every referenced function name.
    assert metric.numerator == 1


def test_ground_truth_coverage_parametric_does_not_cover_state_vars():
    gt = (
        m.GroundTruthProperty(
            contract="Token",
            name="p1",
            referenced_functions=frozenset({"transfer"}),
            referenced_state_vars=frozenset({"totalSupply"}),
        ),
    )
    rules = (
        m.GeneratedRule(
            contract="Token",
            name="param",
            quantifies_all_methods=True,  # covers functions, not state vars
        ),
    )
    metric = m.compute_ground_truth_coverage(gt, rules)
    assert metric.numerator == 0


def test_ground_truth_coverage_excludes_properties_with_no_referenced_names():
    gt = (
        m.GroundTruthProperty(contract="Token", name="empty"),  # no references
        m.GroundTruthProperty(
            contract="Token",
            name="p1",
            referenced_functions=frozenset({"transfer"}),
        ),
    )
    rules = (
        m.GeneratedRule(
            contract="Token",
            name="g1",
            referenced_functions=frozenset({"transfer"}),
        ),
    )
    metric = m.compute_ground_truth_coverage(gt, rules)
    # Only the property declaring >=1 referenced name counts in the denominator.
    assert metric.denominator == 1
    assert metric.numerator == 1


def test_ground_truth_coverage_null_when_absent():
    metric = m.compute_ground_truth_coverage((), ())
    assert metric.is_null
    assert metric.reason == m.REASON_NO_INPUT


def test_ground_truth_coverage_null_when_no_qualifying_properties():
    gt = (m.GroundTruthProperty(contract="Token", name="empty"),)
    metric = m.compute_ground_truth_coverage(gt, ())
    assert metric.is_null
    assert metric.denominator == 0
    assert metric.reason == m.REASON_EMPTY_DENOMINATOR


# ---------------------------------------------------------------------------
# rules_per_gated_function — R12.6 + R12.12 aggregate
# ---------------------------------------------------------------------------


def test_rules_per_gated_function_counts_each_rule_once_per_cohort():
    cohorts = (
        m.ModifierCohort(
            contract="Vault",
            modifier="onlyOwner",
            gated_functions=frozenset({"pause", "unpause"}),
        ),
    )
    rules = (
        # Names the modifier -> references cohort.
        m.GeneratedRule(
            contract="Vault",
            name="r1",
            referenced_modifiers=frozenset({"onlyOwner"}),
        ),
        # Names a gated function AND the modifier -> still counted once.
        m.GeneratedRule(
            contract="Vault",
            name="r2",
            referenced_functions=frozenset({"pause"}),
            referenced_modifiers=frozenset({"onlyOwner"}),
        ),
        # Unrelated rule -> not counted.
        m.GeneratedRule(
            contract="Vault",
            name="r3",
            referenced_functions=frozenset({"deposit"}),
        ),
    )
    per_cohort, aggregate = m.compute_rules_per_gated_function(cohorts, rules)
    assert len(per_cohort) == 1
    cm = per_cohort[0]
    assert cm.numerator == 2  # r1, r2 (each once)
    assert cm.denominator == 2
    assert cm.value == Decimal("1.0000")
    # cohort has < 4 gated functions -> not in aggregate -> null.
    assert aggregate.is_null


def test_rules_per_gated_function_parametric_references_cohort():
    cohorts = (
        m.ModifierCohort(
            contract="Vault",
            modifier="onlyOwner",
            gated_functions=frozenset({"a", "b"}),
        ),
    )
    rules = (m.GeneratedRule(contract="Vault", name="p", quantifies_all_methods=True),)
    per_cohort, _ = m.compute_rules_per_gated_function(cohorts, rules)
    assert per_cohort[0].numerator == 1


def test_rules_per_gated_function_collapsed_to_one_parametric_is_low():
    # Ideal case: one parametric rule for a 4-function cohort -> 1/4 = 0.25.
    cohorts = (
        m.ModifierCohort(
            contract="Vault",
            modifier="onlyOwner",
            gated_functions=frozenset({"a", "b", "c", "d"}),
        ),
    )
    rules = (m.GeneratedRule(contract="Vault", name="p", quantifies_all_methods=True),)
    per_cohort, aggregate = m.compute_rules_per_gated_function(cohorts, rules)
    assert per_cohort[0].value == Decimal("0.2500")
    # >=4 gated fns -> included in aggregate.
    assert aggregate.numerator == 1
    assert aggregate.denominator == 4
    assert aggregate.value == Decimal("0.2500")


def test_rules_per_gated_function_aggregate_only_large_cohorts():
    cohorts = (
        m.ModifierCohort(
            contract="A",
            modifier="onlyOwner",
            gated_functions=frozenset({"a", "b", "c", "d", "e"}),  # >=4
        ),
        m.ModifierCohort(
            contract="B",
            modifier="onlyAdmin",
            gated_functions=frozenset({"x", "y"}),  # < 4, excluded from aggregate
        ),
    )
    rules = (
        m.GeneratedRule(
            contract="A", name="r", referenced_modifiers=frozenset({"onlyOwner"})
        ),
        m.GeneratedRule(
            contract="A", name="r2", referenced_functions=frozenset({"a"})
        ),
        m.GeneratedRule(
            contract="B", name="r3", referenced_modifiers=frozenset({"onlyAdmin"})
        ),
    )
    per_cohort, aggregate = m.compute_rules_per_gated_function(cohorts, rules)
    # Aggregate counts only cohort A (5 gated fns): 2 rules / 5.
    assert aggregate.numerator == 2
    assert aggregate.denominator == 5
    assert aggregate.value == Decimal("0.4000")


def test_rules_per_gated_function_cohort_zero_denominator_is_null():
    cohorts = (
        m.ModifierCohort(contract="A", modifier="onlyOwner", gated_functions=frozenset()),
    )
    per_cohort, aggregate = m.compute_rules_per_gated_function(cohorts, ())
    assert per_cohort[0].is_null if hasattr(per_cohort[0], "is_null") else True
    assert per_cohort[0].value is None
    assert per_cohort[0].reason == m.REASON_EMPTY_DENOMINATOR


def test_rules_per_gated_function_null_aggregate_when_absent():
    per_cohort, aggregate = m.compute_rules_per_gated_function((), ())
    assert per_cohort == ()
    assert aggregate.is_null
    assert aggregate.reason == m.REASON_NO_INPUT


def test_rules_per_gated_function_sorted_deterministically():
    cohorts = (
        m.ModifierCohort(contract="Z", modifier="m2", gated_functions=frozenset({"a"})),
        m.ModifierCohort(contract="A", modifier="m1", gated_functions=frozenset({"b"})),
    )
    per_cohort, _ = m.compute_rules_per_gated_function(cohorts, ())
    keys = [(c.contract, c.modifier) for c in per_cohort]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Telemetry — R12.7 (null-not-zero token counts recorded verbatim)
# ---------------------------------------------------------------------------


def test_telemetry_recorded_verbatim_with_null_tokens():
    tele = (
        m.PairTelemetry(
            pair_id="a",
            llm_calls_provider=3,
            llm_calls_cache=1,
            prompt_tokens=None,  # provider omitted -> null, not 0
            completion_tokens=None,
            stage_durations=(m.StageDuration(1, 0.5), m.StageDuration(3, 2.0)),
        ),
    )
    report = m.compute_metrics(m.EvaluationInputs(telemetry=tele))
    assert report.telemetry == tele
    t0 = report.telemetry[0]
    assert t0.prompt_tokens is None
    assert t0.completion_tokens is None
    assert t0.llm_calls_provider == 3
    assert t0.llm_calls_cache == 1
    assert t0.stage_durations[0].seconds == 0.5


# ---------------------------------------------------------------------------
# Full report + excluded counts — R12.11
# ---------------------------------------------------------------------------


def test_compute_metrics_records_excluded_counts():
    reports = (
        _rep("a", verdicts=[m.RuleVerdict("r1", "passing")]),
        _rep("b", status="tool_unavailable"),
        _rep("c", status="not_run"),
        _rep("d", status=None),
    )
    report = m.compute_metrics(m.EvaluationInputs(pair_reports=reports))
    assert report.excluded_counts.get("tool_unavailable") == 1
    # not_run status + missing status both map to the not_run reason.
    assert report.excluded_counts.get("not_run") == 2


def test_compute_metrics_all_absent_yields_null_metrics():
    report = m.compute_metrics(m.EvaluationInputs())
    assert report.syntax_validity_rate.is_null
    assert report.verdict_rate.is_null
    assert report.vacuity_rate.is_null
    assert report.effective_pass_rate.is_null
    assert report.ground_truth_coverage.is_null
    assert report.rules_per_gated_function == ()
    assert report.rules_per_gated_function_aggregate.is_null
    # Reasons come from the Outcome_Set.
    for metric in (
        report.syntax_validity_rate,
        report.verdict_rate,
        report.ground_truth_coverage,
    ):
        assert metric.reason == m.REASON_NO_INPUT


def test_as_dict_serialization_roundtrips_values_as_strings():
    specs = (m.SpecTypecheck("a", "accepted"), m.SpecTypecheck("b", "rejected"))
    report = m.compute_metrics(m.EvaluationInputs(spec_typechecks=specs))
    d = report.as_dict()
    assert d["syntax_validity_rate"]["value"] == "0.5000"
    assert d["syntax_validity_rate"]["numerator"] == 1
    assert d["syntax_validity_rate"]["denominator"] == 2
    assert d["verdict_rate"]["value"] is None
