"""Property 9 — `verified` is an honest status (R20.11, task 7.2).

**Validates: Requirements 20.11**

For all generated Verification_Reports, a Verification_Status of ``verified``
implies a rule count of at least one, a passing verdict for every rule, a
Vacuous_Rule count of zero, and zero warnings.

The report is produced by driving the real classifier
(:func:`_classify_status`) with generated verdict lists, declared-rule sets,
and warning lists — no real certoraRun. This exercises the classifier's own
guarantee rather than asserting the invariant against a hand-built report.

``spec_pipeline/__init__.py`` eagerly imports slither-backed stages, so
``stage5_verify.py`` is loaded directly via importlib to stay toolchain-free.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_stage5():
    mod_name = "spec_pipeline_stage5_verify_prop_under_test"
    path = _REPO_ROOT / "spec_pipeline" / "stage5_verify.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    from spec_pipeline import stage5_verify as S  # type: ignore
except Exception:  # pragma: no cover - slither absent
    S = _load_stage5()


_VERDICT = st.sampled_from(["PASSED", "FAILED", "VACUOUS", "DEAD", "TIMEOUT", "ERROR"])
_RULE_NAME = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=6,
)


@st.composite
def _rules(draw):
    names = draw(st.lists(_RULE_NAME, min_size=0, max_size=6, unique=True))
    return [{"name": n, "status": draw(_VERDICT)} for n in names]


@st.composite
def _report_inputs(draw):
    rules = draw(_rules())
    verdict_names = {r["name"] for r in rules}
    # Declared rules: sometimes exactly the verdict names, sometimes a superset
    # (introducing a declared-but-no-verdict rule), sometimes None.
    mode = draw(st.integers(min_value=0, max_value=2))
    if mode == 0:
        declared = set(verdict_names)
    elif mode == 1:
        extra = draw(st.lists(_RULE_NAME, min_size=0, max_size=2, unique=True))
        declared = set(verdict_names) | set(extra)
    else:
        declared = None
    warnings = draw(st.lists(st.text(max_size=8), min_size=0, max_size=3))
    return rules, declared, warnings


@settings(max_examples=200)
@given(_report_inputs())
def test_verified_status_is_honest(inputs):
    rules, declared, warnings = inputs
    status = S._classify_status(
        rules,
        tool_available=True,
        declared_rules=declared,
        warnings=warnings,
    )
    report = {"status": status, "rules": rules, "warnings": warnings}

    if status == "verified":
        # R20.11: rule count >= 1, all passing, zero vacuous, zero warnings.
        assert len(rules) >= 1
        assert all(r["status"] == "PASSED" for r in rules)
        assert not any(r["status"] in {"VACUOUS", "DEAD"} for r in rules)
        assert not warnings
        # And a verdict exists for every declared rule.
        if declared is not None:
            assert declared.issubset({r["name"] for r in rules})

    # The report-level invariant checker agrees for every generated report.
    assert S.is_verified_honest(report)


@settings(max_examples=100)
@given(_report_inputs())
def test_verified_with_warnings_implies_all_pass_and_a_warning(inputs):
    rules, declared, warnings = inputs
    status = S._classify_status(
        rules, tool_available=True, declared_rules=declared, warnings=warnings
    )
    if status == "verified_with_warnings":
        assert len(rules) >= 1
        assert all(r["status"] == "PASSED" for r in rules)
        assert warnings
