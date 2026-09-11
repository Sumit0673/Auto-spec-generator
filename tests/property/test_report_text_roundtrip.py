"""Property 5 — Verification_Report printer/parser round-trip (R20.9, task 6.7).

**Validates: Requirements 20.9**

For all Verification_Reports, ``render_report_text`` (the certoraRun-text
printer) followed by ``parse_report_text`` (the inverse over the existing text
parser) yields a report whose Verification_Status, rule-name set, per-rule
verdict, and Vacuous_Rule set equal the original.

Generator coverage (R23.3): every verdict value including ``VACUOUS`` (and
``DEAD``, the other member of the Vacuous_Rule set), zero-rule reports, and
warning lines.

Rule-name constraint (noted): the ``Rule '<name>' STATUS`` text line — and the
vacuity / dead-code lines the parser understands — capture the name with the
regexes ``'([^']+)'`` and ``'?([^'\\s]+)'?``. A name containing a single quote
or whitespace cannot survive that line format, so generated names are
constrained to a safe charset (lowercase letters, digits, underscore) that the
line format round-trips exactly. R23.3 asks the *report* generator to also
cover quote-bearing names; that is exercised elsewhere against fields that do
not go through the text line format. Here the property is specifically the
text round-trip, so names are held to the charset the text format can carry.

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
    mod_name = "spec_pipeline_stage5_verify_rt_under_test"
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


# Every verdict value the report can carry (R23.3).
_VERDICT = st.sampled_from(["PASSED", "FAILED", "VACUOUS", "DEAD", "TIMEOUT", "ERROR"])

# Safe rule-name charset the text line format round-trips exactly (see module
# docstring): no quote, no whitespace.
_RULE_NAME = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789_",
    ),
    min_size=1,
    max_size=8,
)

# Warning messages: single-line, non-empty after stripping, no leading marker
# that _parse_warnings would swallow into an empty message.
_WARNING = st.text(
    alphabet=st.characters(
        whitelist_characters="abcdefghijklmnopqrstuvwxyz ",
        whitelist_categories=(),
    ),
    min_size=1,
    max_size=12,
).map(lambda s: s.strip()).filter(lambda s: len(s) >= 1)


@st.composite
def _reports(draw):
    """Generate a self-consistent Verification_Report.

    The status is derived from the generated rules + warnings with the real
    classifier, so the report is exactly the shape this module produces (and
    thus the shape the text round-trip must preserve).
    """
    names = draw(st.lists(_RULE_NAME, min_size=0, max_size=6, unique=True))
    rules = [{"name": n, "status": draw(_VERDICT)} for n in names]
    warnings = draw(st.lists(_WARNING, min_size=0, max_size=3, unique=True))
    status = S._classify_status(
        rules, tool_available=True, declared_rules=None, warnings=warnings
    )
    return {"status": status, "rules": rules, "warnings": warnings}


def _verdicts(report):
    return {r["name"]: r["status"] for r in report.get("rules") or []}


@settings(max_examples=200)
@given(_reports())
def test_print_then_parse_preserves_the_r20_9_fields(report):
    text = S.render_report_text(report)
    parsed = S.parse_report_text(text)

    # Verification_Status preserved.
    assert parsed["status"] == report["status"]
    # Rule-name set preserved.
    assert {r["name"] for r in parsed["rules"]} == {r["name"] for r in report["rules"]}
    # Per-rule verdict preserved.
    assert _verdicts(parsed) == _verdicts(report)
    # Vacuous_Rule set preserved (VACUOUS and DEAD).
    assert S.vacuous_rule_names(parsed) == S.vacuous_rule_names(report)


@settings(max_examples=100)
@given(_reports())
def test_zero_rule_reports_round_trip(report):
    # Exercise the zero-rule shape explicitly among the generated reports.
    if report["rules"]:
        return
    text = S.render_report_text(report)
    parsed = S.parse_report_text(text)
    assert parsed["status"] == report["status"] == "not_run"
    assert parsed["rules"] == []
