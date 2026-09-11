"""Property 16 — Metrics order-independence (Requirement 12.8).

Permuting the metric input records yields equal numerator, denominator, and
quotient for every metric (including per-cohort and the >=4 aggregate), plus
equal excluded counts. The per-cohort list is compared as an order-independent
set of (contract, modifier, numerator, denominator, value) tuples because the
generator produces cohorts in arbitrary order.

The module under test is import-safe without slither; we load it by file path
via importlib (mirroring the other property tests) and fall back to a plain
import.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_metrics():
    mod_name = "spec_pipeline_metrics_prop_under_test"
    path = _REPO_ROOT / "spec_pipeline" / "eval" / "metrics.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    from spec_pipeline.eval import metrics as m  # type: ignore
except Exception:  # pragma: no cover - slither absent
    m = _load_metrics()


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_IDENT = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=4,
)
_CONTRACT = st.sampled_from(["Token", "Vault", "Pool", "Registry"])
_MODIFIER = st.sampled_from(["onlyOwner", "onlyAdmin", "onlyGov"])
_VERDICT = st.sampled_from(["passing", "failing", "vacuous"])
_REPORT_STATUS = st.sampled_from(
    ["verified", "violated", "vacuous", "tool_unavailable", "not_run", None]
)
_TYPECHECK = st.sampled_from(["accepted", "rejected", "no_result"])


@st.composite
def _spec_typechecks(draw):
    n = draw(st.integers(min_value=0, max_value=6))
    return tuple(
        m.SpecTypecheck(pair_id=f"p{i}", result=draw(_TYPECHECK)) for i in range(n)
    )


@st.composite
def _pair_reports(draw):
    n = draw(st.integers(min_value=0, max_value=5))
    reports = []
    for i in range(n):
        n_v = draw(st.integers(min_value=0, max_value=4))
        verdicts = tuple(
            m.RuleVerdict(name=f"r{i}_{j}", verdict=draw(_VERDICT)) for j in range(n_v)
        )
        reports.append(
            m.PairReport(
                pair_id=f"p{i}",
                scored=draw(st.booleans()),
                report_status=draw(_REPORT_STATUS),
                verdicts=verdicts,
            )
        )
    return tuple(reports)


@st.composite
def _name_set(draw):
    names = draw(st.lists(_IDENT, min_size=0, max_size=3, unique=True))
    return frozenset(names)


@st.composite
def _ground_truth(draw):
    n = draw(st.integers(min_value=0, max_value=5))
    return tuple(
        m.GroundTruthProperty(
            contract=draw(_CONTRACT),
            name=f"gt{i}",
            referenced_functions=draw(_name_set()),
            referenced_state_vars=draw(_name_set()),
        )
        for i in range(n)
    )


@st.composite
def _generated_rules(draw):
    n = draw(st.integers(min_value=0, max_value=6))
    return tuple(
        m.GeneratedRule(
            contract=draw(_CONTRACT),
            name=f"g{i}",
            referenced_functions=draw(_name_set()),
            referenced_state_vars=draw(_name_set()),
            referenced_modifiers=frozenset(
                draw(st.lists(_MODIFIER, min_size=0, max_size=2, unique=True))
            ),
            quantifies_all_methods=draw(st.booleans()),
        )
        for i in range(n)
    )


@st.composite
def _cohorts(draw):
    n = draw(st.integers(min_value=0, max_value=4))
    cohorts = []
    seen = set()
    for _ in range(n):
        contract = draw(_CONTRACT)
        modifier = draw(_MODIFIER)
        if (contract, modifier) in seen:
            continue
        seen.add((contract, modifier))
        gated = draw(st.lists(_IDENT, min_size=0, max_size=6, unique=True))
        cohorts.append(
            m.ModifierCohort(
                contract=contract,
                modifier=modifier,
                gated_functions=frozenset(gated),
            )
        )
    return tuple(cohorts)


@st.composite
def _evaluation_inputs(draw):
    return m.EvaluationInputs(
        spec_typechecks=draw(_spec_typechecks()),
        pair_reports=draw(_pair_reports()),
        ground_truth=draw(_ground_truth()),
        generated_rules=draw(_generated_rules()),
        cohorts=draw(_cohorts()),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metric_triple(metric):
    return (metric.numerator, metric.denominator, metric.value, metric.reason)


def _cohort_set(per_cohort):
    return frozenset(
        (c.contract, c.modifier, c.numerator, c.denominator, c.value, c.reason)
        for c in per_cohort
    )


def _shuffled(seq, seed):
    items = list(seq)
    random.Random(seed).shuffle(items)
    return tuple(items)


def _permute(inputs, seed):
    return m.EvaluationInputs(
        spec_typechecks=_shuffled(inputs.spec_typechecks, seed + 1),
        pair_reports=_shuffled(inputs.pair_reports, seed + 2),
        ground_truth=_shuffled(inputs.ground_truth, seed + 3),
        generated_rules=_shuffled(inputs.generated_rules, seed + 4),
        cohorts=_shuffled(inputs.cohorts, seed + 5),
    )


# ---------------------------------------------------------------------------
# Property 16 — order independence (>=100 examples per conftest 'ci' profile)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(_evaluation_inputs(), st.integers(min_value=0, max_value=10_000))
def test_metrics_order_independent(inputs, seed):
    base = m.compute_metrics(inputs)
    permuted = m.compute_metrics(_permute(inputs, seed))

    # Scalar metrics: equal numerator/denominator/quotient/reason.
    for attr in (
        "syntax_validity_rate",
        "verdict_rate",
        "vacuity_rate",
        "effective_pass_rate",
        "ground_truth_coverage",
        "rules_per_gated_function_aggregate",
    ):
        assert _metric_triple(getattr(base, attr)) == _metric_triple(
            getattr(permuted, attr)
        ), attr

    # Per-cohort metrics: equal as an unordered set.
    assert _cohort_set(base.rules_per_gated_function) == _cohort_set(
        permuted.rules_per_gated_function
    )

    # Excluded counts: equal mapping.
    assert base.excluded_counts == permuted.excluded_counts
