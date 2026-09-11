"""Property-based test for Property 3 — evaluation results round-trip (R13.8).

**Validates: Requirements 13.8**

Property 3 (design, "round-trip" class): writing a :class:`HarnessResult` to a
results file and reading it back yields equal metric values and equal per-pair
outcomes. That is, ``read_results(write_results(r, path))`` preserves every
metric's numerator/denominator/quotient/reason and every per-pair record's
Outcome_Set outcome.

This exercises the pure, slither-free serializers
``spec_pipeline.eval.harness.write_results`` / ``read_results`` (and the
``HarnessResult`` / ``MetricReport`` shapes they operate on) over generated
harness results, so it runs fully offline — no slither, no certoraRun, no LLM.
The example-based sibling
``tests/unit/test_harness.py::test_results_file_round_trip_preserves_metrics_and_outcomes``
pins one concrete case; this generalizes it across >=100 generated results with
varied metric values (integer numerator/denominator/quotient triples and
null/None metrics with reason codes) and varied per-pair outcomes, including the
zero-pair and many-pair extremes.

Import-safety
-------------
``spec_pipeline`` may eagerly pull in slither via its package init on some
machines. Mirroring the sibling property tests (``test_repair_loop_properties``)
and ``tests/integration/test_confluence``, slither-free stubs for
``solidity_graph.analyzer`` and the parent packages are registered BEFORE the
harness module is loaded, and the harness is loaded by file path when the
guarded package import is unavailable/slither-tainted.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Offline import: stub slither-backed packages, then import the harness. Prefer
# the guarded package import; fall back to a direct file-path load.
# ---------------------------------------------------------------------------


def _install_stub_packages() -> None:
    if "solidity_graph.analyzer" not in sys.modules:
        analyzer = types.ModuleType("solidity_graph.analyzer")
        analyzer.SolidityAnalyzer = object
        analyzer.SolidityGraph = object
        analyzer.ContractInfo = object
        analyzer.FunctionNode = object
        analyzer._find_solc = lambda *a, **k: None
        analyzer._SHARED_DEPS = Path("/nonexistent-shared-deps")
        analyzer._build_solc_remaps = lambda *a, **k: []
        sys.modules["solidity_graph.analyzer"] = analyzer

    if "solidity_graph" not in sys.modules or not hasattr(
        sys.modules["solidity_graph"], "__path__"
    ):
        sg = types.ModuleType("solidity_graph")
        sg.__path__ = [str(_REPO_ROOT / "solidity_graph")]
        sg.analyzer = sys.modules["solidity_graph.analyzer"]
        sys.modules["solidity_graph"] = sg

    if "spec_pipeline" not in sys.modules or not hasattr(
        sys.modules["spec_pipeline"], "__path__"
    ):
        sp = types.ModuleType("spec_pipeline")
        sp.__path__ = [str(_REPO_ROOT / "spec_pipeline")]
        sys.modules["spec_pipeline"] = sp


def _load_module_by_path(mod_name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_install_stub_packages()

try:  # prefer the guarded package import; fall back to direct-file load
    from spec_pipeline.eval import harness as h  # type: ignore
    from spec_pipeline.eval import metrics as m  # type: ignore
except Exception:  # pragma: no cover - slither absent in the package init
    m = _load_module_by_path(
        "eval_roundtrip_metrics_uut", "spec_pipeline/eval/metrics.py"
    )
    h = _load_module_by_path("eval_roundtrip_harness_uut", "spec_pipeline/eval/harness.py")


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# The Outcome_Set members a per-pair record may carry (see harness.OUTCOME_SET).
_OUTCOME = st.sampled_from(list(h.OUTCOME_SET))

_METRIC_NAMES = (
    "syntax_validity_rate",
    "verdict_rate",
    "vacuity_rate",
    "effective_pass_rate",
    "ground_truth_coverage",
    "rules_per_gated_function_aggregate",
)

# Reason codes a null metric may carry (subset of Outcome_Set used by metrics).
_REASON = st.sampled_from(["error", "not_run", "tool_unavailable"])


@st.composite
def _metric(draw, name: str):
    """A :class:`Metric`: either a computed numerator/denominator/quotient triple
    or a null metric (value=None) with a reason code (R12.9/R12.10)."""
    is_null = draw(st.booleans())
    if is_null:
        numerator = draw(st.integers(min_value=0, max_value=50))
        denominator = draw(st.integers(min_value=0, max_value=50))
        return m.Metric(
            name=name,
            numerator=numerator,
            denominator=denominator,
            value=None,
            reason=draw(_REASON),
        )
    # Computed: a non-zero denominator and the quotient rounded half-up to 4 dp,
    # exactly as the production metric would build it.
    denominator = draw(st.integers(min_value=1, max_value=50))
    numerator = draw(st.integers(min_value=0, max_value=denominator))
    return m.Metric(
        name=name,
        numerator=numerator,
        denominator=denominator,
        value=m.round_half_up_4(numerator, denominator),
        reason=None,
    )


@st.composite
def _cohort_metric(draw):
    """A per-cohort :class:`CohortMetric` (R12.6) — computed or null denominator."""
    contract = draw(st.text(alphabet="ABCabc_", min_size=1, max_size=6))
    modifier = draw(st.text(alphabet="onlyOwner_", min_size=1, max_size=8))
    denominator = draw(st.integers(min_value=0, max_value=12))
    numerator = draw(st.integers(min_value=0, max_value=20))
    if denominator == 0:
        value: Decimal | None = None
        reason: str | None = "not_run"
    else:
        value = m.round_half_up_4(numerator, denominator)
        reason = None
    return m.CohortMetric(
        contract=contract,
        modifier=modifier,
        numerator=numerator,
        denominator=denominator,
        value=value,
        reason=reason,
    )


@st.composite
def _metric_report(draw):
    """A :class:`MetricReport` with varied metric values across all six metrics
    plus a varied per-cohort list and excluded-count map."""
    cohorts = draw(st.lists(_cohort_metric(), min_size=0, max_size=4))
    excluded = draw(
        st.dictionaries(
            keys=st.sampled_from(["tool_unavailable", "not_run"]),
            values=st.integers(min_value=0, max_value=10),
            max_size=2,
        )
    )
    return m.MetricReport(
        syntax_validity_rate=draw(_metric("syntax_validity_rate")),
        verdict_rate=draw(_metric("verdict_rate")),
        vacuity_rate=draw(_metric("vacuity_rate")),
        effective_pass_rate=draw(_metric("effective_pass_rate")),
        ground_truth_coverage=draw(_metric("ground_truth_coverage")),
        rules_per_gated_function=tuple(cohorts),
        rules_per_gated_function_aggregate=draw(
            _metric("rules_per_gated_function_aggregate")
        ),
        telemetry=(),
        excluded_counts=excluded,
    )


@st.composite
def _pair_record(draw, run_id: str, index: int):
    """One :class:`PairRecord` with a varied Outcome_Set outcome, token counts
    (carried as metric inputs), duration, and optional artifact paths."""
    outcome = draw(_OUTCOME)
    repo = draw(st.text(alphabet="repo_abc0123", min_size=1, max_size=8))
    contract = draw(st.text(alphabet="ABCdef", min_size=1, max_size=6))
    # Vary metric inputs: sometimes empty, sometimes a couple of typecheck /
    # pair-report rows (the subset write_results serializes).
    spec_typechecks = tuple(
        {"pair_id": f"{index}-{i}", "result": r}
        for i, r in enumerate(
            draw(
                st.lists(
                    st.sampled_from(["accepted", "rejected", "no_result"]),
                    max_size=3,
                )
            )
        )
    )
    metric_inputs = {"spec_typechecks": list(spec_typechecks), "pair_reports": []}
    return h.PairRecord(
        run_id=run_id,
        repo=repo,
        contract_path=f"src/{contract}{index}.sol",
        outcome=outcome,
        elapsed_s=draw(
            st.floats(min_value=0.0, max_value=1800.0, allow_nan=False, allow_infinity=False)
        ),
        source_fingerprint=draw(
            st.sampled_from(["sha256:absent", "sha256:deadbeef", "sha256:0"])
        ),
        metric_inputs=metric_inputs,
        spec_path=draw(st.one_of(st.none(), st.just("out/generated.spec"))),
        report_path=draw(st.one_of(st.none(), st.just("out/report.json"))),
        traceback_path=draw(st.one_of(st.none(), st.just("out/tb.txt"))),
        error_type=draw(st.one_of(st.none(), st.just("ValueError"))),
        skipped=draw(st.booleans()),
    )


@st.composite
def _harness_result(draw):
    """A full :class:`HarnessResult`: run id, commit flag, a metric report, and
    a list of per-pair records (zero pairs through many pairs)."""
    run_id = draw(
        st.sampled_from(
            ["20240101T000000Z-abc123", "20240506T070809Z-nocommit", "run-x"]
        )
    )
    # Draw a variable number of records (cover the zero-pair and many-pair ends).
    n = draw(st.integers(min_value=0, max_value=8))
    records = [draw(_pair_record(run_id, i)) for i in range(n)]
    return h.HarnessResult(
        run_id=run_id,
        commit_unresolved=draw(st.booleans()),
        records=records,
        metrics=draw(_metric_report()),
        attempted=draw(st.integers(min_value=0, max_value=8)),
        skipped=draw(st.integers(min_value=0, max_value=8)),
        scored=draw(st.integers(min_value=0, max_value=8)),
    )


# ---------------------------------------------------------------------------
# Property 3 — round-trip
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(result=_harness_result())
def test_write_then_read_preserves_metrics_and_outcomes(result, tmp_path_factory):
    """write_results then read_results yields equal metric values and equal
    per-pair outcomes (R13.8, Property 3)."""
    path = tmp_path_factory.mktemp("eval_roundtrip") / "results.json"

    written = h.write_results(result, path)
    payload = h.read_results(written)

    # Per-pair outcomes survive the round-trip, in order.
    round_trip_outcomes = [rec["outcome"] for rec in payload["records"]]
    assert round_trip_outcomes == [rec.outcome for rec in result.records]

    # Metric values survive the round-trip: the parsed metrics equal the
    # source report's own dict projection (numerator/denominator/quotient/reason
    # for every metric, plus the per-cohort list and excluded counts).
    assert payload["metrics"] == result.metrics.as_dict()

    # Writing then reading a second time is stable (idempotent serialization).
    again = h.write_results(result, path)
    assert h.read_results(again) == payload
