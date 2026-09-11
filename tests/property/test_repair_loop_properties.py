"""Property-based test for the Repair_Loop selection invariant (R22.8).

Property 10 (design): the returned spec's passing-non-vacuous count equals the
maximum count observed across the iterations, with no-verdict iterations
(``typecheck_failed`` / ``tool_unavailable`` / ``not_run``) counted as zero.

This exercises the pure, slither-free selection helper
``spec_pipeline.stage3_iterative._select_best_iteration`` over generated
iteration histories, so it runs fully offline (no slither, no certoraRun, no
LLM). slither is not installable here, so the analyzer + parent packages are
stubbed before the module is loaded by file path (same pattern as the
Repair_Loop unit tests).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


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

if "spec_pipeline.stage3_iterative" not in sys.modules:
    itr = _load_module_by_path(
        "spec_pipeline.stage3_iterative", "spec_pipeline/stage3_iterative.py"
    )
else:  # pragma: no cover
    itr = sys.modules["spec_pipeline.stage3_iterative"]


_VERDICT = st.sampled_from(["PASSED", "FAILED", "VACUOUS", "DEAD", "TIMEOUT"])
_STATUS = st.sampled_from(
    ["verified", "violated", "vacuous", "timeout",
     "typecheck_failed", "tool_unavailable", "not_run"]
)


@st.composite
def _iteration_record(draw, number):
    status = draw(_STATUS)
    n_rules = draw(st.integers(min_value=0, max_value=5))
    rules = [
        {"name": f"r{i}", "status": draw(_VERDICT)} for i in range(n_rules)
    ]
    rec = {"iteration": number, "spec": f"spec-{number}",
           "rules": rules, "status": status, "diagnostics": []}
    rec["passing_non_vacuous_count"] = itr._passing_non_vacuous_count(rec)
    rec["typecheck_accepted"] = itr._typecheck_accepted(rec)
    return rec


@st.composite
def _iterations(draw):
    n = draw(st.integers(min_value=1, max_value=6))
    return [draw(_iteration_record(i + 1)) for i in range(n)]


def _first_regression_index(iterations) -> int | None:
    """Index of the first typecheck-rejected iteration that follows an accepted
    one, i.e. the R22.3 regression trigger. ``None`` when no regression."""
    seen_accepted = False
    for idx, it in enumerate(iterations):
        if itr._typecheck_accepted(it):
            seen_accepted = True
        elif seen_accepted:
            return idx
    return None


@settings(max_examples=200)
@given(_iterations())
def test_returned_iteration_passing_count_equals_max(iterations):
    index, _reason = itr._select_best_iteration(iterations)
    returned = iterations[index]

    counts = [itr._passing_non_vacuous_count(it) for it in iterations]
    returned_count = itr._passing_non_vacuous_count(returned)

    regression_idx = _first_regression_index(iterations)

    if regression_idx is None:
        # No typecheck regression: the returned spec's passing-non-vacuous count
        # equals the maximum across all iterations (R22.8, Property 10).
        assert returned_count == max(counts)
        # Ties are resolved toward the earliest iteration: the returned index is
        # the first index that attains the maximum count.
        assert index == counts.index(max(counts))
    else:
        # Regression path (R22.3): selection pins to the LAST typecheck-accepted
        # iteration strictly before the first regression, returning that earlier
        # accepted spec rather than the rejected later one.
        prefix = iterations[:regression_idx]
        accepted_indices = [
            i for i, it in enumerate(prefix) if itr._typecheck_accepted(it)
        ]
        assert accepted_indices  # a regression implies >=1 prior accepted
        assert itr._typecheck_accepted(returned)
        assert index == accepted_indices[-1]


@settings(max_examples=100)
@given(_iterations())
def test_no_verdict_iterations_count_as_zero(iterations):
    """Every no-verdict iteration (typecheck_failed / tool_unavailable /
    not_run) contributes zero to the passing-non-vacuous count, regardless of
    any rule list it carries (R22.8)."""
    for it in iterations:
        if itr._iter_status(it) in itr._NO_VERDICT_STATUSES:
            assert itr._passing_non_vacuous_count(it) == 0
