"""Unit tests for the Evaluation_Harness (Requirement 13).

The module under test, ``spec_pipeline.eval.harness``, is import-safe without
slither: it imports only :mod:`spec_pipeline.eval.pair_index` and
:mod:`spec_pipeline.eval.metrics` at load time and defers the real pipeline to a
lazily-imported default runner. These tests never touch slither / the LLM /
certoraRun: they inject a FAKE runner and drive tiny in-temp-dir Pair_Index
entries.

The harness is loaded through the guarded package import
``from spec_pipeline.eval import harness`` with an importlib-by-path fallback so
the file still loads on a machine whose ``spec_pipeline`` package eagerly pulls
in slither.

Coverage (Requirement 13):
* R13.1  per-pair invocation order + per-entry output-dir naming from repo+contract
* R13.2/R13.10  --jobs validation: 0/17/non-int reject (exit nonzero) before
        invoking; 1..16 accepted
* R13.3  a runner exceeding the budget records ``timeout`` with elapsed, continues
* R13.4  an unhandled runner exception records ``error`` with exception type + a
        captured-traceback path, continues
* R13.5  per-pair record fields incl. absent artifact paths recorded when not produced
* R13.6/R13.11  --resume skips an entry whose record has one Outcome_Set outcome
        AND a matching Source_Fingerprint; re-runs on a mismatch
* R13.8  results file round-trip: write then read yields equal metric values +
        per-pair outcomes
* R13.9/R13.12  run id stamped in per-pair records/results/summary; commit-unresolved
        fallback derives the id from the UTC start and records it
* R13.7  human summary groups by outcome with repo+contract and lists each metric
        with value-or-null+reason
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, rel: str):
    path = _REPO_ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


try:  # prefer the guarded package import; fall back to direct-file load
    from spec_pipeline.eval import harness as h  # type: ignore
    from spec_pipeline.eval import metrics as m  # type: ignore
    from spec_pipeline.eval import pair_index as pi  # type: ignore
except Exception:  # pragma: no cover - slither absent in the package init
    m = _load_module("harness_metrics_uut", "spec_pipeline/eval/metrics.py")
    pi = _load_module("harness_pair_index_uut", "spec_pipeline/eval/pair_index.py")
    h = _load_module("harness_uut", "spec_pipeline/eval/harness.py")


# ---------------------------------------------------------------------------
# Fixtures: a tiny fake Pair_Index over a temp dataset root
# ---------------------------------------------------------------------------


def _make_entry(dataset_root: Path, repo: str, rel_sol: str, body: str = "contract C {}"):
    """Create a real ``.sol`` on disk and return a matching PairEntry.

    A real file lets the harness compute a genuine Source_Fingerprint (used by
    the --resume matching tests) without any Solidity tooling.
    """
    sol = dataset_root / rel_sol
    sol.parent.mkdir(parents=True, exist_ok=True)
    sol.write_text(body, encoding="utf-8")
    name = Path(rel_sol).stem
    return pi.PairEntry(
        repository=repo,
        contract_path=rel_sol,
        spec_path=rel_sol.replace(".sol", ".spec"),
        contract_name=name,
    )


def _runner_ok(outcome="verified", metric_inputs=None):
    """A fake runner that records its call and returns a fixed RunnerResult."""

    calls: list[tuple[str, str]] = []

    def runner(sol_path: Path, output_dir: Path, stop_event: threading.Event):
        calls.append((Path(sol_path).name, Path(output_dir).name))
        return h.RunnerResult(
            outcome=outcome,
            metric_inputs=metric_inputs
            if metric_inputs is not None
            else m.EvaluationInputs(),
        )

    return runner, calls


# ---------------------------------------------------------------------------
# R13.1 - per-pair invocation order + per-entry output dir naming
# ---------------------------------------------------------------------------


def test_invokes_once_per_entry_in_order_with_named_output_dirs(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    entries = [
        _make_entry(ds, "repo_a", "src/Alpha.sol"),
        _make_entry(ds, "repo_b", "src/Beta.sol"),
    ]

    order: list[str] = []

    def runner(sol_path, output_dir, stop_event):
        order.append(Path(output_dir).name)
        return h.RunnerResult(outcome="verified")

    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=entries,
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        commit="abc123",
    )

    # One invocation per entry, in Pair_Index order.
    assert result.attempted == 2
    assert len(result.records) == 2

    # The per-entry output dir name is derived from repo + contract name (R13.1).
    names = h.default_output_dir_names(entries)
    assert "repo_a" in names[0] and "Alpha" in names[0]
    assert "repo_b" in names[1] and "Beta" in names[1]
    assert order == [names[0], names[1]]

    # Each output dir was created.
    assert (tmp_path / "out" / names[0]).is_dir()
    assert (tmp_path / "out" / names[1]).is_dir()


# ---------------------------------------------------------------------------
# R13.2 / R13.10 - --jobs validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "17", "-1", "abc", "1.5"])
def test_jobs_invalid_values_exit_nonzero_before_invoking(bad, tmp_path):
    parser = h.build_arg_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--output-root", str(tmp_path / "o"), "--jobs", bad])
    # argparse exits with code 2 on an invalid argument, before run_harness runs.
    assert exc.value.code != 0


@pytest.mark.parametrize("good", ["1", "8", "16"])
def test_jobs_valid_values_accepted(good, tmp_path):
    parser = h.build_arg_parser()
    args = parser.parse_args(["--output-root", str(tmp_path / "o"), "--jobs", good])
    assert args.jobs == int(good)
    assert h.MIN_JOBS <= args.jobs <= h.MAX_JOBS


# ---------------------------------------------------------------------------
# R13.3 - per-pair budget -> timeout, elapsed recorded, continues
# ---------------------------------------------------------------------------


def test_budget_exceeded_records_timeout_and_continues(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    slow = _make_entry(ds, "repo_a", "src/Slow.sol")
    fast = _make_entry(ds, "repo_b", "src/Fast.sol")

    def runner(sol_path, output_dir, stop_event):
        if Path(sol_path).name == "Slow.sol":
            # Cooperative: wait past the tiny budget, then unwind on the signal.
            stop_event.wait(timeout=5.0)
            return h.RunnerResult(outcome="verified")
        return h.RunnerResult(outcome="verified")

    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=[slow, fast],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        budget_seconds=0.2,
        start_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        commit="c0ffee",
    )

    by_repo = {r.repo: r for r in result.records}
    assert by_repo["repo_a"].outcome == "timeout"
    # Elapsed is recorded and does not exceed the budget for a timeout (R13.3).
    assert by_repo["repo_a"].elapsed_s <= 0.2 + 0.001
    assert by_repo["repo_a"].elapsed_s > 0
    # The remaining entry still ran (R13.3 "continue with the remaining entries").
    assert by_repo["repo_b"].outcome == "verified"


# ---------------------------------------------------------------------------
# R13.4 - unhandled exception -> error with type + traceback path, continues
# ---------------------------------------------------------------------------


def test_runner_exception_records_error_with_traceback_and_continues(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    boom = _make_entry(ds, "repo_a", "src/Boom.sol")
    ok = _make_entry(ds, "repo_b", "src/Ok.sol")

    def runner(sol_path, output_dir, stop_event):
        if Path(sol_path).name == "Boom.sol":
            raise ValueError("kaboom")
        return h.RunnerResult(outcome="verified")

    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=[boom, ok],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        commit="c0ffee",
    )

    by_repo = {r.repo: r for r in result.records}
    err = by_repo["repo_a"]
    assert err.outcome == "error"
    assert err.error_type == "ValueError"  # exception type recorded (R13.4)
    # A traceback file was captured and holds the traceback text.
    assert err.traceback_path is not None
    tb = Path(err.traceback_path)
    assert tb.is_file()
    assert "kaboom" in tb.read_text(encoding="utf-8")
    # The next entry still ran (R13.4 continue).
    assert by_repo["repo_b"].outcome == "verified"


# ---------------------------------------------------------------------------
# R13.5 - per-pair record fields incl. absent artifact paths recorded
# ---------------------------------------------------------------------------


def test_record_fields_include_absent_artifact_paths_when_not_produced(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    entry = _make_entry(ds, "repo_a", "src/Plain.sol")

    # Runner produces no spec / report -> those paths are recorded as absent.
    def runner(sol_path, output_dir, stop_event):
        return h.RunnerResult(outcome="verified", spec_path=None, report_path=None)

    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=[entry],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        commit="c0ffee",
    )

    rec = result.records[0]
    assert rec.repo == "repo_a"
    assert rec.contract_path == "src/Plain.sol"
    assert rec.outcome == "verified"
    assert rec.elapsed_s >= 0
    # A real Source_Fingerprint was computed for the on-disk .sol (R13.5).
    assert rec.source_fingerprint.startswith("sha256:")
    # Absent artifact paths are recorded as None, not omitted (R13.5).
    assert rec.spec_path is None
    assert rec.report_path is None
    assert "spec_path" in rec.to_dict()
    assert "report_path" in rec.to_dict()

    # A spec/report produced by the runner is carried through and recorded.
    spec_file = tmp_path / "out" / "generated.spec"
    spec_file.write_text("rule r {}", encoding="utf-8")

    def runner2(sol_path, output_dir, stop_event):
        return h.RunnerResult(outcome="verified", spec_path=spec_file, report_path=None)

    result2 = h.run_harness(
        output_root=tmp_path / "out2",
        entries=[entry],
        dataset_base=ds,
        runner=runner2,
        jobs=1,
        start_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        commit="c0ffee",
    )
    assert result2.records[0].spec_path == str(spec_file)
    assert result2.records[0].report_path is None


# ---------------------------------------------------------------------------
# R13.6 / R13.11 - --resume skip on match, re-run on mismatch
# ---------------------------------------------------------------------------


def test_resume_skips_matching_fingerprint_and_reruns_on_mismatch(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    entry = _make_entry(ds, "repo_a", "src/Resumed.sol", body="contract C { uint x; }")
    out_root = tmp_path / "out"

    calls = {"n": 0}

    def runner(sol_path, output_dir, stop_event):
        calls["n"] += 1
        return h.RunnerResult(outcome="verified")

    # First run: produces one record with a single Outcome_Set outcome.
    h.run_harness(
        output_root=out_root,
        entries=[entry],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        commit="c0ffee",
    )
    assert calls["n"] == 1

    # Second run with --resume and an UNCHANGED source: the entry is skipped and
    # the runner is not invoked again (R13.6, R13.11).
    result_resume = h.run_harness(
        output_root=out_root,
        entries=[entry],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        resume=True,
        start_utc=datetime(2024, 1, 2, tzinfo=timezone.utc),
        commit="c0ffee",
    )
    assert calls["n"] == 1  # not re-invoked
    assert result_resume.skipped == 1
    assert result_resume.attempted == 0
    carried = result_resume.records[0]
    assert carried.skipped is True
    assert carried.outcome == "verified"

    # Now CHANGE the source so the fingerprint no longer matches: --resume must
    # re-run the entry (R13.11).
    (ds / "src" / "Resumed.sol").write_text(
        "contract C { uint x; uint y; }", encoding="utf-8"
    )
    result_mismatch = h.run_harness(
        output_root=out_root,
        entries=[entry],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        resume=True,
        start_utc=datetime(2024, 1, 3, tzinfo=timezone.utc),
        commit="c0ffee",
    )
    assert calls["n"] == 2  # re-invoked on mismatch
    assert result_mismatch.attempted == 1
    assert result_mismatch.skipped == 0
    assert result_mismatch.records[0].skipped is False


# ---------------------------------------------------------------------------
# R13.8 - results file round-trip
# ---------------------------------------------------------------------------


def test_results_file_round_trip_preserves_metrics_and_outcomes(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    entries = [
        _make_entry(ds, "repo_a", "src/One.sol"),
        _make_entry(ds, "repo_b", "src/Two.sol"),
    ]

    inputs = m.EvaluationInputs(
        spec_typechecks=(
            m.SpecTypecheck(pair_id="one", result="accepted"),
            m.SpecTypecheck(pair_id="two", result="rejected"),
        ),
    )

    def runner(sol_path, output_dir, stop_event):
        return h.RunnerResult(outcome="verified", metric_inputs=inputs)

    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=entries,
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        commit="c0ffee",
    )

    results_path = tmp_path / "out" / "results.json"
    assert results_path.is_file()  # write_results ran as part of run_harness

    payload = h.read_results(results_path)

    # Per-pair outcomes survive the round-trip (R13.8).
    round_trip_outcomes = [r["outcome"] for r in payload["records"]]
    assert round_trip_outcomes == [r.outcome for r in result.records]

    # Metric values survive the round-trip (R13.8).
    assert payload["metrics"] == result.metrics.as_dict()

    # Writing then reading again yields the same payload.
    rewritten = tmp_path / "out" / "results_copy.json"
    h.write_results(result, rewritten)
    assert h.read_results(rewritten) == payload


# ---------------------------------------------------------------------------
# R13.9 - run id stamped everywhere; R13.12 - commit-unresolved fallback
# ---------------------------------------------------------------------------


def test_run_id_stamped_in_records_results_and_summary(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    entry = _make_entry(ds, "repo_a", "src/Id.sol")
    runner, _ = _runner_ok()

    start = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=[entry],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=start,
        commit="deadbee",
    )

    expected_id = "20240506T070809Z-deadbee"
    assert result.run_id == expected_id
    # Stamped in the per-pair record (R13.9).
    assert result.records[0].run_id == expected_id

    # Stamped in the machine-readable results file (R13.9).
    payload = h.read_results(tmp_path / "out" / "results.json")
    assert payload["run_id"] == expected_id
    assert payload["records"][0]["run_id"] == expected_id

    # Stamped in the human-readable summary (R13.9).
    summary_text = (tmp_path / "out" / "summary.txt").read_text(encoding="utf-8")
    assert expected_id in summary_text


def test_commit_unresolved_fallback_uses_utc_start(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    entry = _make_entry(ds, "repo_a", "src/NoCommit.sol")
    runner, _ = _runner_ok()

    start = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    # commit=None and no repo_root -> commit cannot be resolved (R13.12).
    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=[entry],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=start,
        commit=None,
        repo_root=None,
    )

    assert result.commit_unresolved is True
    # The id is derived from the UTC start alone (R13.12).
    assert result.run_id == "20240506T070809Z-nocommit"
    assert result.records[0].run_id == result.run_id

    # The results file records the unresolved-commit flag.
    payload = h.read_results(tmp_path / "out" / "results.json")
    assert payload["commit_unresolved"] is True
    assert payload["run_id"] == "20240506T070809Z-nocommit"

    # The summary notes the unresolved commit.
    summary_text = (tmp_path / "out" / "summary.txt").read_text(encoding="utf-8")
    assert "unresolved" in summary_text.lower()


def test_compute_run_id_helper(tmp_path):
    start = datetime(2024, 12, 31, 23, 59, 58, tzinfo=timezone.utc)
    run_id, unresolved = h.compute_run_id(start, "abcd123")
    assert run_id == "20241231T235958Z-abcd123"
    assert unresolved is False

    run_id2, unresolved2 = h.compute_run_id(start, None)
    assert run_id2 == "20241231T235958Z-nocommit"
    assert unresolved2 is True


# ---------------------------------------------------------------------------
# R13.7 - human summary groups by outcome + lists metrics with value-or-null
# ---------------------------------------------------------------------------


def test_human_summary_groups_by_outcome_and_lists_metrics(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    v1 = _make_entry(ds, "repo_a", "src/V1.sol")
    v2 = _make_entry(ds, "repo_a", "src/V2.sol")
    err = _make_entry(ds, "repo_b", "src/Err.sol")

    def runner(sol_path, output_dir, stop_event):
        if Path(sol_path).name == "Err.sol":
            raise RuntimeError("bad")
        # One accepted spec so a metric has a concrete value; nothing scored for
        # verdict-derived metrics so those stay null with a reason (R13.7).
        return h.RunnerResult(
            outcome="verified",
            metric_inputs=m.EvaluationInputs(
                spec_typechecks=(m.SpecTypecheck(pair_id="p", result="accepted"),),
            ),
        )

    result = h.run_harness(
        output_root=tmp_path / "out",
        entries=[v1, v2, err],
        dataset_base=ds,
        runner=runner,
        jobs=1,
        start_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        commit="c0ffee",
    )

    summary = (tmp_path / "out" / "summary.txt").read_text(encoding="utf-8")

    # Grouped by outcome with counts, listing repo + contract for each pair (R13.7).
    assert "Outcomes:" in summary
    assert "verified (2):" in summary
    assert "error (1):" in summary
    assert "repo_a :: src/V1.sol" in summary
    assert "repo_a :: src/V2.sol" in summary
    assert "repo_b :: src/Err.sol" in summary

    # Each Requirement-12 metric is listed with a value or null+reason (R13.7).
    assert "Metrics:" in summary
    for name in (
        "syntax_validity_rate",
        "verdict_rate",
        "vacuity_rate",
        "effective_pass_rate",
        "ground_truth_coverage",
        "rules_per_gated_function_aggregate",
    ):
        assert name in summary

    # syntax_validity_rate has a concrete value (one accepted spec / one spec).
    assert "syntax_validity_rate: 1" in summary
    # A verdict-derived metric with no scored input is null with a reason.
    assert "verdict_rate: null (reason=" in summary
