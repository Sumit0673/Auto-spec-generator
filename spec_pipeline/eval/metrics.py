"""Metric_Calculator — evaluation metrics over harness results (Requirement 12).

This module is the single owner of the six evaluation metrics plus the per-pair
telemetry record. It is deliberately **import-safe without slither**: it does
not import ``stage1_extract``, the analyzer, or any slither-backed stage. It
computes purely over plain data — small dataclasses defined here that mirror the
shapes the harness (``eval/harness.py``) records per pair. Nothing here couples
to ``pair_index`` internals beyond plain fields (repo, contract, names).

Design notes anchored to the requirements:

* Every metric is reported as an integer *numerator*, an integer *denominator*,
  and a *quotient* rounded half-up to 4 decimal places using
  :class:`decimal.Decimal` (R12.8 wording "two people compute the same number";
  half-up rounding at 4 dp is the deterministic rule).
* When a required input is absent, or when a denominator is zero, the metric is
  emitted as ``value=None`` (null) with a ``reason`` code drawn from the
  Outcome_Set (R12.9, R12.10).
* Verdict-derived metrics exclude pairs whose report is ``tool_unavailable`` or
  whose stage 5 did not run (``not_run``), recording the per-reason excluded
  counts (R12.11).
* Every aggregation is over an *unordered* multiset of records, so permuting the
  inputs yields identical numerator/denominator/quotient (R12.8 / Property 16).

The public entry point is :func:`compute_metrics`, which takes an
:class:`EvaluationInputs` bundle and returns a :class:`MetricReport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

# ---------------------------------------------------------------------------
# Reason codes (a subset of the Outcome_Set used by this module) — R12.9/12.11
# ---------------------------------------------------------------------------

# Emitted when a metric cannot be computed because there is no input at all.
REASON_NO_INPUT = "error"
# Emitted when the denominator is zero (nothing to divide by). Not a tool/skip
# problem — simply nothing measured — so we surface it as ``not_run``.
REASON_EMPTY_DENOMINATOR = "not_run"
# Per-pair exclusion reasons for verdict-derived metrics (R12.11).
REASON_TOOL_UNAVAILABLE = "tool_unavailable"
REASON_NOT_RUN = "not_run"

# Report statuses that exclude a pair from verdict-derived metrics (R12.11).
_EXCLUDED_STATUSES = {
    "tool_unavailable": REASON_TOOL_UNAVAILABLE,
    "not_run": REASON_NOT_RUN,
    None: REASON_NOT_RUN,  # stage 5 was never run for this pair
}


# ---------------------------------------------------------------------------
# Rounding helper (R12.8)
# ---------------------------------------------------------------------------


def round_half_up_4(numerator: int, denominator: int) -> Decimal:
    """Return ``numerator/denominator`` as a Decimal rounded half-up to 4 dp.

    The division is performed in :class:`~decimal.Decimal` space so the result
    is exact before rounding; ``ROUND_HALF_UP`` makes the tie-break rule
    deterministic and reproducible across machines.

    The caller guarantees ``denominator != 0`` (zero-denominator cases are
    handled upstream as null metrics per R12.10).
    """
    quotient = Decimal(int(numerator)) / Decimal(int(denominator))
    return quotient.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Input records (plain data; mirror what the harness records per pair)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecTypecheck:
    """One generated spec's CVL typecheck outcome (R12.1).

    ``result`` is one of ``"accepted"``, ``"rejected"``, or ``"no_result"``.
    Only accepted/rejected specs count toward ``syntax_validity_rate``;
    ``no_result`` specs are excluded from both numerator and denominator.
    """

    pair_id: str
    result: str  # "accepted" | "rejected" | "no_result"


@dataclass(frozen=True)
class RuleVerdict:
    """One rule's prover verdict within a pair's Verification_Report.

    ``verdict`` is one of ``"passing"``, ``"failing"``, ``"vacuous"``. A rule is
    counted as "received a verdict" when it appears here with any of those
    values. ``vacuous`` marks a Vacuous_Rule (a false success): passing only
    because no reachable state meets its preconditions.
    """

    name: str
    verdict: str  # "passing" | "failing" | "vacuous"


@dataclass(frozen=True)
class PairReport:
    """A scored pair's verification result as consumed by the metrics.

    ``report_status`` mirrors the Verification_Report status. Pairs with status
    ``tool_unavailable`` or ``not_run`` (or ``None`` when stage 5 never ran) are
    excluded from verdict-derived metric numerators/denominators (R12.11).

    ``scored`` marks whether the pair was scored at all (it reached a pipeline
    result). ``verdicts`` holds the per-rule verdicts when present.
    """

    pair_id: str
    scored: bool = True
    report_status: Optional[str] = None
    verdicts: tuple[RuleVerdict, ...] = ()


@dataclass(frozen=True)
class GroundTruthProperty:
    """A human-written Ground_Truth_Property and the names it references (R12.5).

    ``referenced_functions`` and ``referenced_state_vars`` are the exact,
    case-sensitive names the property mentions. A GT property "declares >=1
    referenced name" when either set is non-empty; only such properties count
    toward ``ground_truth_coverage``.
    """

    contract: str
    name: str
    referenced_functions: frozenset[str] = frozenset()
    referenced_state_vars: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GeneratedRule:
    """A generated rule and the names it references, for one contract (R12.5/6).

    * ``referenced_functions`` / ``referenced_state_vars``: exact names the rule
      mentions.
    * ``quantifies_all_methods``: True when the rule is parametric over all
      methods (``method f`` with no selector restriction). Such a rule counts as
      referencing *every* function name of its contract (R12.5) and as
      referencing every cohort of that contract (R12.6).
    * ``referenced_modifiers``: access-control modifier names the rule mentions
      by name (used for cohort referencing, R12.6).
    """

    contract: str
    name: str
    referenced_functions: frozenset[str] = frozenset()
    referenced_state_vars: frozenset[str] = frozenset()
    referenced_modifiers: frozenset[str] = frozenset()
    quantifies_all_methods: bool = False


@dataclass(frozen=True)
class ModifierCohort:
    """A Modifier_Cohort: gated functions in one contract sharing a modifier.

    A LOW ``rules_per_gated_function`` is desirable: the ideal is the cohort
    collapsed to ONE parametric rule.
    """

    contract: str
    modifier: str
    gated_functions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StageDuration:
    """Per-stage wall-clock seconds for one pair (R12.7)."""

    stage: int
    seconds: float


@dataclass(frozen=True)
class PairTelemetry:
    """Per-pair telemetry recorded verbatim (R12.7).

    Token counts are ``None`` (not 0) when the provider omitted them
    (``stage5_verify`` returns null when the tool is absent; the same null
    convention applies to provider-omitted token counts).
    """

    pair_id: str
    llm_calls_provider: int = 0  # calls that reached the provider
    llm_calls_cache: int = 0  # calls served from the LLM_Cache
    prompt_tokens: Optional[int] = None  # null (not 0) when provider omitted
    completion_tokens: Optional[int] = None  # null (not 0) when provider omitted
    stage_durations: tuple[StageDuration, ...] = ()


@dataclass(frozen=True)
class EvaluationInputs:
    """The full set of metric inputs for one evaluation run.

    Any collection left empty means "input absent" for the metrics that depend
    on it, which yields a null metric with a reason code (R12.9).
    """

    spec_typechecks: tuple[SpecTypecheck, ...] = ()
    pair_reports: tuple[PairReport, ...] = ()
    ground_truth: tuple[GroundTruthProperty, ...] = ()
    generated_rules: tuple[GeneratedRule, ...] = ()
    cohorts: tuple[ModifierCohort, ...] = ()
    telemetry: tuple[PairTelemetry, ...] = ()


# ---------------------------------------------------------------------------
# Output records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    """One metric value as numerator/denominator/quotient or null+reason.

    Exactly one of the two states holds:

    * a computed metric: ``value`` is a Decimal, ``numerator``/``denominator``
      are ints, ``reason`` is None;
    * a null metric: ``value`` is None, ``reason`` is an Outcome_Set code, and
      ``numerator``/``denominator`` carry whatever counts were available (0 when
      no input existed at all).
    """

    name: str
    numerator: int
    denominator: int
    value: Optional[Decimal]
    reason: Optional[str] = None

    @property
    def is_null(self) -> bool:
        return self.value is None

    def as_dict(self) -> dict:
        """Serialize to plain JSON-friendly data (quotient as a string)."""
        return {
            "name": self.name,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": None if self.value is None else str(self.value),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CohortMetric:
    """``rules_per_gated_function`` for one cohort (R12.6)."""

    contract: str
    modifier: str
    numerator: int
    denominator: int
    value: Optional[Decimal]
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "contract": self.contract,
            "modifier": self.modifier,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": None if self.value is None else str(self.value),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MetricReport:
    """The full metric report for one evaluation run."""

    syntax_validity_rate: Metric
    verdict_rate: Metric
    vacuity_rate: Metric
    effective_pass_rate: Metric
    ground_truth_coverage: Metric
    rules_per_gated_function: tuple[CohortMetric, ...]
    rules_per_gated_function_aggregate: Metric  # cohorts with >=4 gated fns (R12.12)
    telemetry: tuple[PairTelemetry, ...]
    excluded_counts: dict[str, int]  # per-reason excluded pair counts (R12.11)

    def as_dict(self) -> dict:
        return {
            "syntax_validity_rate": self.syntax_validity_rate.as_dict(),
            "verdict_rate": self.verdict_rate.as_dict(),
            "vacuity_rate": self.vacuity_rate.as_dict(),
            "effective_pass_rate": self.effective_pass_rate.as_dict(),
            "ground_truth_coverage": self.ground_truth_coverage.as_dict(),
            "rules_per_gated_function": [
                c.as_dict() for c in self.rules_per_gated_function
            ],
            "rules_per_gated_function_aggregate": (
                self.rules_per_gated_function_aggregate.as_dict()
            ),
            "excluded_counts": dict(self.excluded_counts),
        }


# ---------------------------------------------------------------------------
# Metric construction helpers
# ---------------------------------------------------------------------------


def _metric(
    name: str,
    numerator: int,
    denominator: int,
    *,
    have_input: bool,
) -> Metric:
    """Build a :class:`Metric`, applying the null rules (R12.9, R12.10).

    * ``have_input`` False → null with ``REASON_NO_INPUT`` (input absent).
    * denominator 0 → null with ``REASON_EMPTY_DENOMINATOR`` (R12.10).
    * otherwise → computed quotient rounded half-up to 4 dp.
    """
    if not have_input:
        return Metric(name, numerator, denominator, None, REASON_NO_INPUT)
    if denominator == 0:
        return Metric(name, numerator, denominator, None, REASON_EMPTY_DENOMINATOR)
    return Metric(name, numerator, denominator, round_half_up_4(numerator, denominator))


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------


def compute_syntax_validity_rate(specs: tuple[SpecTypecheck, ...]) -> Metric:
    """specs the typechecker accepted / specs with an accept-or-reject result.

    Specs with ``no_result`` are excluded from both numerator and denominator
    (R12.1). Absent input (no specs at all) → null.
    """
    have_input = len(specs) > 0
    denominator = sum(1 for s in specs if s.result in ("accepted", "rejected"))
    numerator = sum(1 for s in specs if s.result == "accepted")
    return _metric(
        "syntax_validity_rate", numerator, denominator, have_input=have_input
    )


def _verdict_eligible(reports: tuple[PairReport, ...]):
    """Split scored pairs into verdict-eligible and per-reason excluded (R12.11).

    Returns ``(eligible, excluded_counts)`` where ``eligible`` is the list of
    scored pairs whose report status is neither ``tool_unavailable`` nor
    ``not_run`` (nor missing), and ``excluded_counts`` maps each exclusion reason
    to the count of scored pairs excluded for that reason.
    """
    eligible = []
    excluded_counts: dict[str, int] = {}
    for r in reports:
        if not r.scored:
            continue
        if r.report_status in _EXCLUDED_STATUSES:
            reason = _EXCLUDED_STATUSES[r.report_status]
            excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
            continue
        eligible.append(r)
    return eligible, excluded_counts


def compute_verdict_rate(reports: tuple[PairReport, ...]) -> Metric:
    """scored pairs whose report has >=1 rule verdict / scored pairs (R12.2).

    Pairs excluded by ``tool_unavailable`` / ``not_run`` (R12.11) are dropped
    from both numerator and denominator.
    """
    scored = [r for r in reports if r.scored]
    have_input = len(scored) > 0
    eligible, _ = _verdict_eligible(reports)
    denominator = len(eligible)
    numerator = sum(1 for r in eligible if len(r.verdicts) >= 1)
    return _metric("verdict_rate", numerator, denominator, have_input=have_input)


def _rules_with_verdict(reports: tuple[PairReport, ...]) -> list[RuleVerdict]:
    """All rule verdicts across verdict-eligible pairs (R12.3/R12.4 denominator)."""
    eligible, _ = _verdict_eligible(reports)
    rules: list[RuleVerdict] = []
    for r in eligible:
        rules.extend(r.verdicts)
    return rules


def compute_vacuity_rate(reports: tuple[PairReport, ...]) -> Metric:
    """vacuous rules / rules that received a verdict (R12.3)."""
    have_input = any(r.scored for r in reports)
    rules = _rules_with_verdict(reports)
    denominator = len(rules)
    numerator = sum(1 for rv in rules if rv.verdict == "vacuous")
    return _metric("vacuity_rate", numerator, denominator, have_input=have_input)


def compute_effective_pass_rate(reports: tuple[PairReport, ...]) -> Metric:
    """passing-and-non-vacuous rules / rules that received a verdict (R12.4).

    Same denominator as ``vacuity_rate``.
    """
    have_input = any(r.scored for r in reports)
    rules = _rules_with_verdict(reports)
    denominator = len(rules)
    numerator = sum(1 for rv in rules if rv.verdict == "passing")
    return _metric(
        "effective_pass_rate", numerator, denominator, have_input=have_input
    )


def _rules_by_contract(
    rules: tuple[GeneratedRule, ...],
) -> dict[str, list[GeneratedRule]]:
    by_contract: dict[str, list[GeneratedRule]] = {}
    for r in rules:
        by_contract.setdefault(r.contract, []).append(r)
    return by_contract


def compute_ground_truth_coverage(
    ground_truth: tuple[GroundTruthProperty, ...],
    generated_rules: tuple[GeneratedRule, ...],
) -> Metric:
    """covered GT properties / GT properties declaring >=1 referenced name (R12.5).

    A GT property is *covered* iff every referenced function name AND every
    referenced state-var name appears (exact, case-sensitive) in >=1 generated
    rule for the *same contract*. A generated rule that quantifies over all
    methods counts as referencing every function name for its contract.
    """
    have_input = len(ground_truth) > 0
    by_contract = _rules_by_contract(generated_rules)

    denominator = 0
    numerator = 0
    for gt in ground_truth:
        if not gt.referenced_functions and not gt.referenced_state_vars:
            continue  # only GT properties declaring >=1 referenced name count
        denominator += 1

        contract_rules = by_contract.get(gt.contract, [])
        # Function names referenced across the contract's generated rules. A
        # parametric rule over all methods covers every function name, so if any
        # such rule exists, all referenced function names are satisfied.
        any_parametric = any(r.quantifies_all_methods for r in contract_rules)
        covered_functions: set[str] = set()
        covered_state_vars: set[str] = set()
        for r in contract_rules:
            covered_functions |= set(r.referenced_functions)
            covered_state_vars |= set(r.referenced_state_vars)

        functions_ok = any_parametric or gt.referenced_functions <= covered_functions
        state_vars_ok = gt.referenced_state_vars <= covered_state_vars
        if functions_ok and state_vars_ok:
            numerator += 1

    return _metric(
        "ground_truth_coverage", numerator, denominator, have_input=have_input
    )


def _rule_references_cohort(rule: GeneratedRule, cohort: ModifierCohort) -> bool:
    """A rule references a cohort if it (R12.6):

    * names the cohort modifier, OR
    * names >=1 gated function of the cohort, OR
    * quantifies over the contract's methods.
    """
    if rule.contract != cohort.contract:
        return False
    if rule.quantifies_all_methods:
        return True
    if cohort.modifier in rule.referenced_modifiers:
        return True
    if set(rule.referenced_functions) & set(cohort.gated_functions):
        return True
    return False


def compute_rules_per_gated_function(
    cohorts: tuple[ModifierCohort, ...],
    generated_rules: tuple[GeneratedRule, ...],
) -> tuple[tuple[CohortMetric, ...], Metric]:
    """Per-cohort ``rules_per_gated_function`` and the >=4 aggregate (R12.6/12.12).

    For each cohort: generated rules referencing the cohort / gated functions in
    the cohort. Each rule is counted once per cohort. The aggregate sums
    numerators and denominators over cohorts with >=4 gated functions.

    Returns ``(per_cohort, aggregate)``. Per-cohort metrics are ordered by
    (contract, modifier) so the output is deterministic regardless of input
    order.
    """
    have_input = len(cohorts) > 0
    by_contract = _rules_by_contract(generated_rules)

    per_cohort: list[CohortMetric] = []
    agg_numerator = 0
    agg_denominator = 0
    agg_has_large_cohort = False

    for cohort in cohorts:
        contract_rules = by_contract.get(cohort.contract, [])
        # Count each rule once per cohort.
        numerator = sum(
            1 for r in contract_rules if _rule_references_cohort(r, cohort)
        )
        denominator = len(cohort.gated_functions)
        if denominator == 0:
            value = None
            reason: Optional[str] = REASON_EMPTY_DENOMINATOR
        else:
            value = round_half_up_4(numerator, denominator)
            reason = None
        per_cohort.append(
            CohortMetric(
                contract=cohort.contract,
                modifier=cohort.modifier,
                numerator=numerator,
                denominator=denominator,
                value=value,
                reason=reason,
            )
        )
        if denominator >= 4:
            agg_has_large_cohort = True
            agg_numerator += numerator
            agg_denominator += denominator

    per_cohort.sort(key=lambda c: (c.contract, c.modifier))

    # The aggregate has input only when at least one cohort has >=4 gated fns.
    aggregate = _metric(
        "rules_per_gated_function_aggregate",
        agg_numerator,
        agg_denominator,
        have_input=have_input and agg_has_large_cohort,
    )
    return tuple(per_cohort), aggregate


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def compute_metrics(inputs: EvaluationInputs) -> MetricReport:
    """Compute the full :class:`MetricReport` from an :class:`EvaluationInputs`.

    Order-independent (R12.8): every metric aggregates over unordered
    collections, so permuting the input records yields an equal report (equal
    numerator/denominator/quotient for each metric). The only ordered output is
    the per-cohort list, which is sorted by (contract, modifier).
    """
    _, excluded_counts = _verdict_eligible(inputs.pair_reports)

    per_cohort, aggregate = compute_rules_per_gated_function(
        inputs.cohorts, inputs.generated_rules
    )

    return MetricReport(
        syntax_validity_rate=compute_syntax_validity_rate(inputs.spec_typechecks),
        verdict_rate=compute_verdict_rate(inputs.pair_reports),
        vacuity_rate=compute_vacuity_rate(inputs.pair_reports),
        effective_pass_rate=compute_effective_pass_rate(inputs.pair_reports),
        ground_truth_coverage=compute_ground_truth_coverage(
            inputs.ground_truth, inputs.generated_rules
        ),
        rules_per_gated_function=per_cohort,
        rules_per_gated_function_aggregate=aggregate,
        telemetry=tuple(inputs.telemetry),
        excluded_counts=excluded_counts,
    )
