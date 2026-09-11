"""Suite-level completeness check for the property test suite (task 13.5).

**Validates: Requirements 23.4, 23.5, 23.6, 23.7, 23.8, 23.10, 23.11**

The design (``design.md`` — "Property classes") enumerates the property tests
that MUST exist, grouped by property class, and requires each to evaluate at
least 100 generated examples (R23.10). This meta-test asserts that contract by
*introspection* rather than by re-running the properties:

* **round-trip** — R2 (Stage 1 artifact), R11 (Pair_Index), R13 (evaluation
  results), R18 (CVL extraction), R20 (Verification_Report printer/parser)
* **idempotence** — R17 (methods block), R18 (CVL extraction)
* **invariant** — R17 (methods block completeness), R20 (``verified`` honest),
  R22 (repair loop maximum)
* **metamorphic** — R7 (path relocation), R15 (prompt alpha-rename), R19
  (dependency-add / file-subset)
* **confluence** — R4 (stage sequencing equivalence)
* **order-independence** — R12 (metrics order-independence)

For every property that is present, this test locates the file and the exact
test function and asserts its *effective* ``max_examples`` is at least 100. The
effective value is either the inline ``@settings(max_examples=N)`` on the test,
or — when a ``@given`` test carries no inline ``max_examples`` — the
``max_examples`` of the hypothesis profile loaded suite-wide by
``tests/conftest.py`` (the ``ci`` profile, 100 examples).

Introspection is done by **static source parsing** (reading the file text and
walking its AST). The property modules import slither-backed code paths through
importlib fallbacks; parsing the source keeps this meta-test fully offline and
independent of whether slither is installed (R23.11 / R10.2). This test is
order-independent: it discovers everything from the filesystem at call time and
holds no cross-test state.

R23.11 additionally requires that *tool-facing behavior* (slither / solc /
certoraRun / LLM) is exercised only by small fixture-backed integration tests
(1–3 recorded examples), never by a ``@given`` property that drives a live tool
across ≥100 examples. This test asserts the integration files that carry that
tool-facing behavior exist and contain no ≥100-example hypothesis property.

Known gaps: none. Every design property class now has a ≥100-example
hypothesis (``@given``) property test, so the ``_MISSING_PROPERTIES`` list below
is empty and the strict-xfail meta-test generates no cases. R4 (stage sequencing
equivalence / confluence, Property 15) was the last remaining gap and is now
supplied by ``tests/property/test_stage_confluence_properties.py``, promoted
into ``_PRESENT_PROPERTIES`` under the ``confluence`` class. The strict-xfail
scaffolding is retained (with an empty parametrize list) so a future gap can be
recorded again without reintroducing the machinery.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent.parent
_PROPERTY_DIR = _TESTS_DIR / "property"
_UNIT_DIR = _TESTS_DIR / "unit"
_INTEGRATION_DIR = _TESTS_DIR / "integration"

_MIN_EXAMPLES = 100


# ---------------------------------------------------------------------------
# Hypothesis profile default (fallback when a @given test has no inline
# max_examples). Reading it from the loaded profile keeps the fallback honest
# instead of hard-coding 100. Hypothesis is a pure-python test dep — importing
# it triggers no slither/solc/network path.
# ---------------------------------------------------------------------------


def _profile_max_examples() -> int:
    """Return max_examples of the currently loaded hypothesis profile.

    ``tests/conftest.py`` registers and loads the ``ci`` profile with
    ``max_examples=100``. If hypothesis is unavailable we conservatively return
    0 so a fallback-only test cannot silently satisfy the ≥100 requirement.
    """
    try:
        from hypothesis import settings

        return int(settings().max_examples)
    except Exception:  # pragma: no cover - hypothesis absent
        return 0


# ---------------------------------------------------------------------------
# Static introspection of a test file: find a @given test function and compute
# its effective max_examples from the @settings decorator (or the profile).
# ---------------------------------------------------------------------------


class _PropertyTestInfo:
    def __init__(self, exists: bool, has_given: bool, inline_max: "int | None"):
        self.exists = exists
        self.has_given = has_given
        self.inline_max = inline_max

    @property
    def effective_max(self) -> int:
        if not self.has_given:
            return 0
        if self.inline_max is not None:
            return self.inline_max
        return _profile_max_examples()


def _decorator_name(node: ast.expr) -> str:
    """Return the callable/name of a decorator node (``settings`` / ``given``)."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _settings_max_examples(dec: ast.Call) -> "int | None":
    """Extract the literal ``max_examples`` kwarg from a ``@settings(...)`` call."""
    for kw in dec.keywords:
        if kw.arg == "max_examples" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, int):
                return kw.value.value
    return None


def _inspect_property_test(path: Path, func_name: str) -> _PropertyTestInfo:
    """Statically inspect *func_name* in *path* for @given and effective max_examples."""
    if not path.is_file():
        return _PropertyTestInfo(exists=False, has_given=False, inline_max=None)

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        has_given = False
        inline_max: "int | None" = None
        has_settings = False
        for dec in node.decorator_list:
            name = _decorator_name(dec)
            if name == "given":
                has_given = True
            elif name == "settings":
                has_settings = True
                if isinstance(dec, ast.Call):
                    found = _settings_max_examples(dec)
                    if found is not None:
                        inline_max = found
        # A @settings with no max_examples kwarg means "inherit the profile".
        if has_settings and inline_max is None:
            inline_max = None
        return _PropertyTestInfo(
            exists=True, has_given=has_given, inline_max=inline_max
        )
    return _PropertyTestInfo(exists=True, has_given=False, inline_max=None)


def _file_has_hundred_example_property(path: Path) -> bool:
    """True if *path* defines any @given test whose effective max_examples >= 100."""
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    profile_default = _profile_max_examples()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        has_given = any(
            _decorator_name(d) == "given" for d in node.decorator_list
        )
        if not has_given:
            continue
        inline = None
        for dec in node.decorator_list:
            if _decorator_name(dec) == "settings" and isinstance(dec, ast.Call):
                inline = _settings_max_examples(dec)
        effective = inline if inline is not None else profile_default
        if effective >= _MIN_EXAMPLES:
            return True
    return False


# ---------------------------------------------------------------------------
# The required property -> (file, test function, requirement id) mapping.
#
# Each entry names the class, requirement, the property-test file, and the
# test function that implements it. Entries verified present by listing and
# grepping the suite (task 13.5).
# ---------------------------------------------------------------------------

# (class, requirement, path, test_function)
_PRESENT_PROPERTIES = [
    # round-trip
    (
        "round-trip",
        "R2",
        _PROPERTY_DIR / "test_artifacts_stage1_roundtrip.py",
        "test_stage1_artifact_round_trip",
    ),
    (
        "round-trip",
        "R11",
        _PROPERTY_DIR / "test_pair_index_properties.py",
        "test_pair_index_write_read_round_trip",
    ),
    (
        "round-trip",
        "R20",
        _PROPERTY_DIR / "test_report_text_roundtrip.py",
        "test_print_then_parse_preserves_the_r20_9_fields",
    ),
    (
        "round-trip",
        "R18",
        _PROPERTY_DIR / "test_cvl_extraction_properties.py",
        "test_cvl_fence_round_trips_unchanged",
    ),
    (
        "round-trip",
        "R13",
        _PROPERTY_DIR / "test_eval_results_roundtrip.py",
        "test_write_then_read_preserves_metrics_and_outcomes",
    ),
    # idempotence
    (
        "idempotence",
        "R17",
        _PROPERTY_DIR / "test_methods_block_properties.py",
        "test_methods_block_idempotent",
    ),
    (
        "idempotence",
        "R18",
        _PROPERTY_DIR / "test_cvl_extraction_properties.py",
        "test_extract_is_idempotent",
    ),
    # invariant
    (
        "invariant",
        "R17",
        _PROPERTY_DIR / "test_methods_block_properties.py",
        "test_methods_block_completeness_invariant",
    ),
    (
        "invariant",
        "R20",
        _PROPERTY_DIR / "test_verified_honest_invariant.py",
        "test_verified_status_is_honest",
    ),
    (
        "invariant",
        "R22",
        _PROPERTY_DIR / "test_repair_loop_properties.py",
        "test_returned_iteration_passing_count_equals_max",
    ),
    # metamorphic
    (
        "metamorphic",
        "R7",
        _PROPERTY_DIR / "test_path_relocation_metamorphic.py",
        "test_relocation_yields_field_equal_artifacts_after_relativization",
    ),
    (
        "metamorphic",
        "R15",
        _PROPERTY_DIR / "test_prompt_alpha_renaming.py",
        "test_prompt_alpha_renaming_metamorphic",
    ),
    (
        "metamorphic",
        "R19-depadd",
        _PROPERTY_DIR / "test_stage1_generality_properties.py",
        "test_dependency_file_addition_leaves_table_unchanged",
    ),
    (
        "metamorphic",
        "R19-subset",
        _PROPERTY_DIR / "test_stage1_generality_properties.py",
        "test_file_subset_yields_subset_contract_set",
    ),
    # order-independence
    (
        "order-independence",
        "R12",
        _PROPERTY_DIR / "test_metrics_order_independence.py",
        "test_metrics_order_independent",
    ),
    # confluence
    (
        "confluence",
        "R4",
        _PROPERTY_DIR / "test_stage_confluence_properties.py",
        "test_confluence_one_shot_equals_five_single_stage",
    ),
]


# Property classes required by the design that currently have NO hypothesis
# property test (only example-based unit/integration coverage). Each is asserted
# absent and reported; xfail(strict=True) keeps the suite green while surfacing
# the gap and flipping to a failure the moment a real property test is added.
# (class, requirement, expected_property_file, existing_example_based_cover)
#
# This list is currently EMPTY: every design property class now has a >=100
# example hypothesis property test. R4 (stage sequencing equivalence /
# confluence) was the last gap and is now present in _PRESENT_PROPERTIES
# (``tests/property/test_stage_confluence_properties.py``). An empty parametrize
# list generates no cases for the strict-xfail meta-test below, which is the
# intended state; add an entry here only if a future design property class ships
# without its >=100-example hypothesis test.
_MISSING_PROPERTIES: list = []


# Integration files that carry the tool-facing behavior (slither/solc/
# certoraRun/LLM), which R23.11 requires to be fixture-backed and small — never
# ≥100-example live-tool properties.
_TOOL_FACING_INTEGRATION = [
    _INTEGRATION_DIR / "test_end_to_end.py",
    _INTEGRATION_DIR / "test_tool_absent_run.py",
    _INTEGRATION_DIR / "test_confluence.py",
    _INTEGRATION_DIR / "test_corpus_generality.py",
]


# ---------------------------------------------------------------------------
# Present properties: exist + effective max_examples >= 100.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prop_class,requirement,path,func",
    _PRESENT_PROPERTIES,
    ids=[f"{c}:{r}" for (c, r, _p, _f) in _PRESENT_PROPERTIES],
)
def test_required_property_exists_and_runs_at_least_100_examples(
    prop_class, requirement, path, func
):
    info = _inspect_property_test(path, func)
    assert info.exists, (
        f"[{prop_class} / {requirement}] property-test file is missing: {path}"
    )
    assert info.has_given, (
        f"[{prop_class} / {requirement}] {path.name}::{func} exists but is not a "
        f"hypothesis @given property test"
    )
    assert info.effective_max >= _MIN_EXAMPLES, (
        f"[{prop_class} / {requirement}] {path.name}::{func} evaluates only "
        f"{info.effective_max} examples; the design requires >= {_MIN_EXAMPLES} "
        f"(R23.10)"
    )


def test_present_property_classes_are_all_covered():
    """Every design property class has at least one present ≥100-example test.

    Confluence (R4) is now present — ``tests/property/
    test_stage_confluence_properties.py`` supplies the ≥100-example hypothesis
    property — so it is included in the expected coverage set below. The missing
    set is now empty.
    """
    covered = {c for (c, _r, _p, _f) in _PRESENT_PROPERTIES}
    expected = {
        "round-trip",
        "idempotence",
        "invariant",
        "metamorphic",
        "order-independence",
        "confluence",
    }
    assert expected.issubset(covered), (
        f"present property classes {sorted(covered)} do not cover all of "
        f"{sorted(expected)}"
    )


# ---------------------------------------------------------------------------
# Missing properties: recorded as strict xfails so the gap stays visible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prop_class,requirement,expected_prop_file,example_based_file",
    _MISSING_PROPERTIES,
    ids=[f"{c}:{r}" for (c, r, _e, _x) in _MISSING_PROPERTIES],
)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Required design property class has no >=100-example hypothesis property "
        "test yet; only example-based coverage exists. Tracked for a follow-up fix. "
        "Remove the xfail once the property test is added."
    ),
)
def test_missing_property_has_hundred_example_hypothesis_test(
    prop_class, requirement, expected_prop_file, example_based_file
):
    # The example-based coverage must at least exist (so the behavior is not
    # wholly untested) ...
    assert example_based_file.is_file(), (
        f"[{prop_class} / {requirement}] expected example-based coverage at "
        f"{example_based_file} is missing entirely"
    )
    # ... and this assertion FAILS today (no ≥100-example property test),
    # producing the expected xfail. When someone adds the property test, this
    # passes unexpectedly and strict=True flips it to a failure, prompting the
    # maintainer to promote it into _PRESENT_PROPERTIES.
    assert _file_has_hundred_example_property(expected_prop_file), (
        f"[{prop_class} / {requirement}] no >=100-example hypothesis property "
        f"test found at {expected_prop_file}"
    )


# ---------------------------------------------------------------------------
# R23.11: tool-facing behavior is fixture-backed integration only, never a
# ≥100-example live-tool property.
# ---------------------------------------------------------------------------


def test_tool_facing_behavior_is_fixture_backed_integration_only():
    """The integration tests exercising slither/solc/certoraRun/LLM exist and
    are NOT ≥100-example hypothesis properties over live tools (R23.11)."""
    present = [p for p in _TOOL_FACING_INTEGRATION if p.is_file()]
    assert present, (
        "no tool-facing integration tests found under tests/integration/; "
        "R23.11 requires tool behavior to be covered by fixture-backed "
        "integration tests"
    )
    offenders = [p.name for p in present if _file_has_hundred_example_property(p)]
    assert not offenders, (
        "tool-facing integration files must be small fixture-backed tests, not "
        f">=100-example hypothesis properties over live tools (R23.11): {offenders}"
    )


def test_property_directory_holds_no_live_tool_property():
    """No property test drives a real external tool: the whole property suite
    stays offline (R23.11 / R10.2). Property files import tool-backed modules
    only through importlib fallbacks used to read pure text/serialization code;
    none shells out to solc/certoraRun/slither. This guards against a future
    property test smuggling a live-tool invocation behind a @given.
    """
    banned = ("subprocess.run", "subprocess.Popen", "subprocess.check_")
    for path in sorted(_PROPERTY_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, (
                f"{path.name} appears to invoke an external process ({token}); "
                f"property tests must stay offline (R23.11)"
            )
