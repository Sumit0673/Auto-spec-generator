"""Property-based tests for CVL extraction (Requirements 18.5, 18.6, 23.2).

Two design properties over ``spec_pipeline.cvl.extract_cvl`` (the
CVL_Extractor):

* **Property 4 — round-trip (R18.5, R18.6).** A ```cvl-fenced document
  extracts UNCHANGED apart from surrounding-whitespace stripping and an
  (optional) prepended methods block. Formally, for a CVL body ``doc`` with no
  leading/trailing whitespace::

      extract_cvl("```cvl\\n" + doc + "\\n```") == doc

  and every semantics-bearing fragment the model wrote (``<=``, ``!= 0``,
  ``!= address(0)``, ``:=`` hook assignments, ``invariant`` declarations,
  ``using`` lines, ``!hasRole`` negations) survives byte-for-byte — the deleted
  destructive rewrites MUST NOT reappear (R18.5).

* **Property 7 — idempotence (R18.7).** ``extract_cvl(extract_cvl(x)) ==
  extract_cvl(x)`` for arbitrary raw documents (fenced and unfenced), with no
  methods block.

The module under test is pure regex/text handling with no slither-tainted
import chain, so a direct import works here. To stay robust against a future
``spec_pipeline/__init__.py`` that eagerly imports the slither-backed stages,
we stub the ``solidity_graph.analyzer`` chain and fall back to a file-path
import (same offline pattern as the sibling property tests, e.g.
``tests/property/test_repair_loop_properties.py``).

Fully offline: no slither, no solc, no certoraRun, no LLM, no network. The two
properties are order-independent; they hold no cross-test state.
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


try:  # cvl.py is slither-free, so a direct import should work.
    from spec_pipeline import cvl as _cvl  # type: ignore
except Exception:  # pragma: no cover - slither-tainted __init__ fallback
    _install_stub_packages()
    if "spec_pipeline.cvl" in sys.modules:
        _cvl = sys.modules["spec_pipeline.cvl"]
    else:
        _cvl = _load_module_by_path("spec_pipeline.cvl", "spec_pipeline/cvl.py")

extract_cvl = _cvl.extract_cvl


# ---------------------------------------------------------------------------
# CVL document generator.
#
# We build CVL bodies out of the declaration forms the extractor must preserve:
# rules, parametric rules, invariants, hooks (including ``:=`` which must NOT be
# rewritten), ghosts, ``using`` declarations, comments, and nested braces. The
# semantics-bearing operators (``<=``, ``!= 0``, ``!= address(0)``) are
# deliberately seeded so the round-trip test can assert they survive.
#
# Constraint: bodies must NOT contain a triple-backtick, otherwise they would
# close the surrounding ```cvl fence early and the round-trip identity would no
# longer be a property of the extractor but of the fence. We keep identifiers
# and operators backtick-free by construction.
# ---------------------------------------------------------------------------

_IDENT = st.from_regex(r"[a-zA-Z_][a-zA-Z0-9_]{0,10}", fullmatch=True)


# Fragments that carry the semantics the extractor must never rewrite.
_SEMANTIC_STMTS = st.sampled_from(
    [
        "assume x <= y;",
        "require a <= b;",
        "assert amount != 0;",
        "require balance != 0;",
        "assume owner != address(0);",
        "assert to != address(0);",
        "assume !hasRole(ADMIN, e.msg.sender);",
        "require a >= b;",
        "assert e != f;",
        "// a trailing comment != 0 <= stays put",
    ]
)


@st.composite
def _rule(draw):
    name = draw(_IDENT)
    body = draw(st.lists(_SEMANTIC_STMTS, min_size=1, max_size=4))
    inner = "\n    ".join(body)
    return f"rule {name} {{\n    {inner}\n    assert true;\n}}"


@st.composite
def _parametric_rule(draw):
    name = draw(_IDENT)
    method = draw(_IDENT)
    body = draw(st.lists(_SEMANTIC_STMTS, min_size=0, max_size=3))
    inner = "\n    ".join(body)
    sep = "\n    " if inner else ""
    return (
        f"rule {name}(method f) filtered {{ f -> f.selector == {method}.selector }} {{\n"
        f"    env e;{sep}{inner}\n"
        f"    assert true;\n}}"
    )


@st.composite
def _invariant(draw):
    name = draw(_IDENT)
    getter = draw(_IDENT)
    return f"invariant {name}() {getter}() >= 0;"


@st.composite
def _tuple_invariant(draw):
    name = draw(_IDENT)
    a = draw(_IDENT)
    b = draw(_IDENT)
    return f"invariant {name}() ({a}(), {b}()) == ({b}(), {a}());"


@st.composite
def _hook(draw):
    slot = draw(_IDENT)
    var = draw(_IDENT)
    # ``:=`` MUST be preserved verbatim (not rewritten to ``=``).
    return (
        f"hook Sstore {slot} uint256 newValue {{\n"
        f"    {var} := newValue;\n"
        f"}}"
    )


@st.composite
def _ghost(draw):
    name = draw(_IDENT)
    return f"ghost mapping(address => uint256) {name};"


@st.composite
def _using(draw):
    contract = draw(_IDENT)
    alias = draw(_IDENT)
    return f"using {contract} as {alias};"


@st.composite
def _comment(draw):
    text = draw(st.from_regex(r"[a-zA-Z0-9 ,.<=!()]{0,30}", fullmatch=True))
    return f"// {text}"


_DECLARATIONS = st.one_of(
    _rule(),
    _parametric_rule(),
    _invariant(),
    _tuple_invariant(),
    _hook(),
    _ghost(),
    _using(),
    _comment(),
)


@st.composite
def cvl_documents(draw):
    """Generate a CVL body: a sequence of declarations joined by blank lines.

    The result never contains a triple-backtick and never has leading/trailing
    whitespace, so it is a fixed point of ``.strip()`` and can be wrapped in a
    ```cvl fence for the round-trip identity.
    """
    parts = draw(st.lists(_DECLARATIONS, min_size=1, max_size=5))
    doc = "\n\n".join(parts)
    # Guarantee the invariants the round-trip identity relies on.
    assert "```" not in doc
    return doc.strip()


def _fence_cvl(doc: str) -> str:
    return f"```cvl\n{doc}\n```"


# Raw documents for the idempotence property: both fenced and unfenced, and
# with arbitrary surrounding whitespace / surrounding prose.
@st.composite
def raw_documents(draw):
    doc = draw(cvl_documents())
    shape = draw(st.integers(min_value=0, max_value=4))
    lead = draw(st.sampled_from(["", "  ", "\n\n", "   \n "]))
    trail = draw(st.sampled_from(["", "  ", "\n\n", " \n  "]))
    if shape == 0:
        return lead + _fence_cvl(doc) + trail
    if shape == 1:
        # Fenced with surrounding prose (the extractor keeps only the body).
        return f"Here is the spec:\n{lead}{_fence_cvl(doc)}{trail}\nthanks!"
    if shape == 2:
        # Generic (non-cvl) fence whose body carries CVL keywords.
        return f"prose\n```\n{doc}\n```\nmore prose"
    if shape == 3:
        # Unfenced document (whole-text fallback).
        return lead + doc + trail
    # Empty-ish / whitespace only occasionally.
    return lead + doc


# ---------------------------------------------------------------------------
# Property 4 — round-trip (R18.5, R18.6)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(cvl_documents())
def test_cvl_fence_round_trips_unchanged(doc):
    """extract_cvl of a ```cvl-fenced body returns the body verbatim.

    **Validates: Requirements 18.5**
    """
    fenced = _fence_cvl(doc)
    assert extract_cvl(fenced) == doc


@settings(max_examples=200)
@given(cvl_documents())
def test_round_trip_preserves_semantic_operators(doc):
    """Every semantics-bearing fragment present in the input survives byte-for-
    byte in the output — the deleted destructive rewrites never reappear.

    **Validates: Requirements 18.5**
    """
    out = extract_cvl(_fence_cvl(doc))

    # For each destructive rewrite that used to exist: if the source contains
    # the original fragment, the output keeps it AND does not contain the
    # rewritten form derived from it.
    if "<=" in doc:
        assert "<=" in out
    if "!= 0" in doc:
        assert "!= 0" in out
    if "!= address(0)" in doc:
        assert "!= address(0)" in out
        assert "> address(0)" not in out
    if "!hasRole" in doc:
        assert "!hasRole" in out
        assert "not hasRole" not in out
    if ":=" in doc:
        assert ":=" in out
    if "invariant " in doc:
        # No invariant declaration is deleted.
        assert doc.count("invariant ") == out.count("invariant ")
    if "using " in doc:
        assert doc.count("using ") == out.count("using ")


@settings(max_examples=200)
@given(cvl_documents(), _IDENT, _IDENT)
def test_round_trip_with_methods_block_prepends_only(doc, getter, ret):
    """With a methods block, extraction prepends exactly that block (separated
    by a blank line) and leaves the body otherwise unchanged (R18.6).

    **Validates: Requirements 18.6**
    """
    methods_block = (
        "methods {\n"
        f"    function {getter}() external returns (address) envfree;\n"
        "}"
    )
    out = extract_cvl(_fence_cvl(doc), methods_block=methods_block)

    expected = methods_block + "\n\n" + doc
    assert out == expected
    # Stripping the known prepended block recovers the clean round-trip body.
    assert out[len(methods_block) + 2:] == doc


# ---------------------------------------------------------------------------
# Property 7 — idempotence (R18.7)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(raw_documents())
def test_extract_is_idempotent(raw):
    """extract_cvl(extract_cvl(x)) == extract_cvl(x) for fenced and unfenced
    documents, with no methods block (R18.7).

    **Validates: Requirements 18.5**
    """
    once = extract_cvl(raw)
    twice = extract_cvl(once)
    assert twice == once
