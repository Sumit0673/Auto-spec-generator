"""Property 12 — Prompt alpha-renaming metamorphic (R15.7, task 12.2).

**Validates: Requirements 15.7**

An injective identifier renaming applied to BOTH the analyzed source and the
Stage 1 table changes the assembled prompt ONLY at the renamed positions. The
prompt template literals are project-agnostic (R15.1-R15.3): every concrete
identifier in the assembled prompt originates from the source or the Stage 1
table, so renaming those inputs must produce a prompt that is the original
prompt with the same substitution applied and nothing else changed.

Metamorphic relation asserted here::

    build_prompt(rename(source), rename(table)) == rename(build_prompt(source, table))

where ``rename`` is a whole-word identifier substitution over an injective map.

Order-preservation
------------------
Some prompt sections sort by identifier (``build_cohort_guidance`` sorts
modifiers; ``build_syntax_example`` sorts candidate function gates by name). If
a renaming reordered those keys the prompt would differ at more than the renamed
positions and the relation would (correctly) fail even though nothing is wrong
with the builder. To isolate the property we want (identifiers flow through
unchanged apart from the rename) the renaming used here is **order-preserving**:
each distinct original identifier is mapped, in sorted order, to a fixed-width
token ``idNNNN``. Because the targets sort identically to the sources, every
sort in the builder yields the same order before and after renaming, so any
residual difference is a genuine leak of a non-renamed identifier position.

The ``idNNNN`` targets also share no substring with the template boilerplate
(which uses ``counter`` / ``increment`` / ``env`` / CVL keywords), so applying
the same whole-word substitution to the ORIGINAL prompt only touches the
identifier positions and never the boilerplate.

Offline: ``spec_pipeline/__init__.py`` eagerly imports slither-backed stages, so
``prompts.py`` (import-safe on its own) is loaded directly via importlib to stay
toolchain-free.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_prompts():
    mod_name = "spec_pipeline_prompts_alpha_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    path = _REPO_ROOT / "spec_pipeline" / "prompts.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    from spec_pipeline import prompts as P  # type: ignore
except Exception:  # pragma: no cover - slither absent
    P = _load_prompts()


# ---------------------------------------------------------------------------
# Duck-typed Stage 1 stand-ins (slither-free), matching stage1_extract shapes.
# ---------------------------------------------------------------------------


@dataclass
class SV:
    name: str
    type: str
    visibility: str = "public"
    is_constant: bool = False
    is_immutable: bool = False
    writers: list = field(default_factory=list)


@dataclass
class FG:
    name: str
    signature: str
    visibility: str = "external"
    mutability: str = "nonpayable"
    modifier: str = "none"
    is_constructor: bool = False
    is_fallback: bool = False
    is_receive: bool = False


@dataclass
class Contract:
    name: str
    kind: str = "contract"
    source_file: str = "C.sol"
    state_vars: list = field(default_factory=list)
    function_gates: list = field(default_factory=list)
    caller_edges: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Identifier alphabet + generators
# ---------------------------------------------------------------------------
#
# Identifiers live in a private ``Zz``-prefixed namespace so they can never
# collide with ANY token in the prompt boilerplate. The boilerplate mixes
# lowercase placeholders (``counter``/``increment``/``env``) and ALL-CAPS words
# (``STAGE``/``TABLE``/``DISPATCHER``/``DEFAULT_ADMIN_ROLE``); the ``Zz`` prefix
# followed by lowercase letters matches none of them. Every generated name is
# therefore a genuine "project identifier" that must survive the rename
# transparently, and we can collect exactly the project identifiers by matching
# the ``\bZz[a-z]*\b`` shape.

_IDENT = st.builds(
    lambda body: "Zz" + body,
    st.text(
        alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
        min_size=1,
        max_size=6,
    ),
)

_PRIM_TYPE = st.sampled_from(["uint256", "address", "bool", "bytes32", "uint256[]"])
_MUTABILITY = st.sampled_from(["view", "pure", "nonpayable", "payable"])


@st.composite
def _function_gate(draw):
    name = draw(_IDENT)
    n_params = draw(st.integers(min_value=0, max_value=3))
    params = [f"{draw(_PRIM_TYPE)} p{i}" for i in range(n_params)]
    ret = draw(st.sampled_from(["", "bool", "uint256", "address"]))
    sig = "(" + ", ".join(params) + ")"
    if ret:
        sig += f" -> {ret}"
    return FG(
        name=name,
        signature=sig,
        visibility=draw(st.sampled_from(["public", "external"])),
        mutability=draw(_MUTABILITY),
        modifier=draw(st.sampled_from(["none", draw(_IDENT)])),
    )


@st.composite
def _contract(draw, name):
    gates = draw(st.lists(_function_gate(), min_size=0, max_size=4))
    svs = draw(
        st.lists(
            st.builds(SV, name=_IDENT, type=_PRIM_TYPE),
            min_size=0,
            max_size=3,
        )
    )
    return Contract(name=name, state_vars=svs, function_gates=gates)


@st.composite
def _table(draw):
    cnames = draw(st.lists(_IDENT, min_size=1, max_size=3, unique=True))
    contracts = {cn: draw(_contract(cn)) for cn in cnames}

    class Table:
        def __init__(self, contracts):
            self.contracts = contracts
            self.project_path = "/proj"

    return Table(contracts)


# ---------------------------------------------------------------------------
# Order-preserving injective renaming over the [A-Z]+ identifier tokens.
# ---------------------------------------------------------------------------

# Project identifiers are exactly the ``Zz``-prefixed lowercase tokens. Nothing
# in the boilerplate matches this shape. We use a leading word boundary plus a
# ``(?![a-z])`` tail so an occurrence GLUED to a following CamelCase suffix (the
# syntax-example builder emits ``rule <fn>CallExample()``, gluing ``Zza`` to
# ``CallExample``) is still recognized as an identifier occurrence, while
# ``Zza`` is never matched inside a longer lowercase identifier like ``Zzab``.
_IDENT_TOKEN_RE = re.compile(r"\bZz[a-z]*(?![a-z])")


def _collect_project_identifiers(*texts: str) -> list[str]:
    """Return the sorted distinct project identifiers (our ``Zz...`` tokens)."""
    found: set[str] = set()
    for text in texts:
        for tok in _IDENT_TOKEN_RE.findall(text or ""):
            found.add(tok)
    return sorted(found)


def _build_rename_map(identifiers: list[str]) -> dict[str, str]:
    """Order-preserving injective map: i-th sorted identifier -> ``idNNNN``.

    Fixed-width zero-padded targets sort in the same order as their 0-based
    index, so ``a < b  =>  rename(a) < rename(b)`` for the sorted inputs. The
    ``id`` prefix + digits share no whole-word token with the boilerplate.
    """
    return {name: f"id{i:04d}" for i, name in enumerate(identifiers)}


def _apply_rename_text(text: str, mapping: dict[str, str]) -> str:
    """Apply the whole-word identifier substitution to a text blob."""
    if not text:
        return text

    def repl(m: "re.Match[str]") -> str:
        return mapping.get(m.group(0), m.group(0))

    # Match our project identifier tokens as whole words only.
    return _IDENT_TOKEN_RE.sub(repl, text)


def _rename_signature(sig: str, mapping: dict[str, str]) -> str:
    """Rename identifiers inside a function signature, preserving punctuation."""
    def repl(m: "re.Match[str]") -> str:
        return mapping.get(m.group(0), m.group(0))

    return _IDENT_TOKEN_RE.sub(repl, sig)


def _rename_contract(c: Contract, mapping: dict[str, str]) -> Contract:
    return Contract(
        name=mapping.get(c.name, c.name),
        kind=c.kind,
        source_file=c.source_file,
        state_vars=[
            SV(
                name=mapping.get(sv.name, sv.name),
                type=_rename_signature(sv.type, mapping),
                visibility=sv.visibility,
                is_constant=sv.is_constant,
                is_immutable=sv.is_immutable,
                writers=[mapping.get(w, w) for w in sv.writers],
            )
            for sv in c.state_vars
        ],
        function_gates=[
            FG(
                name=mapping.get(g.name, g.name),
                signature=_rename_signature(g.signature, mapping),
                visibility=g.visibility,
                mutability=g.mutability,
                modifier=mapping.get(g.modifier, g.modifier),
                is_constructor=g.is_constructor,
                is_fallback=g.is_fallback,
                is_receive=g.is_receive,
            )
            for g in c.function_gates
        ],
        caller_edges=list(c.caller_edges),
    )


def _rename_table(table, mapping: dict[str, str]):
    class Table:
        def __init__(self, contracts):
            self.contracts = contracts
            self.project_path = "/proj"

    return Table(
        {
            mapping.get(cn, cn): _rename_contract(c, mapping)
            for cn, c in table.contracts.items()
        }
    )


# ---------------------------------------------------------------------------
# Stage 1 table text renderer (mirrors Stage1Table.to_text, duck-typed).
# ---------------------------------------------------------------------------


def _render_table_text(table) -> str:
    lines: list[str] = []
    for cname, contract in table.contracts.items():
        lines.append(f"=== Contract: {cname} ===")
        lines.append(f"Kind: {contract.kind}")
        lines.append(f"Source: {contract.source_file}")
        lines.append("")
        if contract.state_vars:
            lines.append("State Variables (writer = function that writes):")
            for sv in contract.state_vars:
                writers = ", ".join(sv.writers) if sv.writers else "(none)"
                lines.append(f"  {sv.name} : {sv.type} | writers: {writers}")
            lines.append("")
        if contract.function_gates:
            lines.append("External/Public Functions (access gate):")
            for fg in contract.function_gates:
                lines.append(f"  {fg.name}{fg.signature} : {fg.modifier}")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt assembly under test (Stage 3 user prompt exercises the most
# identifier-bearing sections: table text, source, methods block, type/cohort
# guidance, and syntax example).
# ---------------------------------------------------------------------------


def _pick_contract(table):
    """Return (name, entry) for the sorted-first contract, matching the builder."""
    name = sorted(table.contracts)[0]
    return name, table.contracts[name]


def _build_prompt(table, source: str) -> str:
    table_text = _render_table_text(table)
    _cname, entry = _pick_contract(table)
    # A tiny methods block carrying only project identifiers.
    methods_block = "methods {\n" + "\n".join(
        f"    function {g.name}{g.signature} external;"
        for g in entry.function_gates
        if not (g.is_constructor or g.is_fallback or g.is_receive)
    ) + "\n}"
    return P.format_stage3_user(
        stage1_table_text=table_text,
        stage2_invariants={},
        source_code=source,
        methods_block=methods_block,
        detected_types=[],
        contract_entry=entry,
    )


def _make_source(table) -> str:
    """Synthesize a small source blob referencing the table identifiers."""
    parts: list[str] = []
    for cname, c in table.contracts.items():
        parts.append(f"contract {cname} {{")
        for sv in c.state_vars:
            parts.append(f"    {sv.type} public {sv.name};")
        for g in c.function_gates:
            parts.append(f"    function {g.name}{g.signature} {{}}")
        parts.append("}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Property 12 (R15.7)
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(_table())
def test_prompt_alpha_renaming_metamorphic(table):
    source = _make_source(table)

    original_prompt = _build_prompt(table, source)

    # Build the order-preserving injective renaming from every project
    # identifier that appears in the assembled prompt.
    identifiers = _collect_project_identifiers(original_prompt)
    mapping = _build_rename_map(identifiers)

    # Rename the INPUTS and rebuild.
    renamed_table = _rename_table(table, mapping)
    renamed_source = _apply_rename_text(source, mapping)
    prompt_from_renamed_inputs = _build_prompt(renamed_table, renamed_source)

    # Rename the ORIGINAL prompt with the SAME substitution.
    renamed_original_prompt = _apply_rename_text(original_prompt, mapping)

    # Metamorphic relation: renaming the inputs changes the prompt only at the
    # renamed positions, i.e. it equals renaming the assembled prompt directly.
    assert prompt_from_renamed_inputs == renamed_original_prompt


@settings(max_examples=150)
@given(_table())
def test_prompt_alpha_renaming_touches_only_identifiers(table):
    """The renamed and original prompts differ only where identifiers sit.

    Masking every project identifier (originals AND their targets) to a common
    sentinel must collapse the two prompts to identical text: all other
    characters (the boilerplate) are untouched by the rename.
    """
    source = _make_source(table)
    original_prompt = _build_prompt(table, source)
    identifiers = _collect_project_identifiers(original_prompt)
    mapping = _build_rename_map(identifiers)

    renamed_table = _rename_table(table, mapping)
    renamed_source = _apply_rename_text(source, mapping)
    prompt_from_renamed_inputs = _build_prompt(renamed_table, renamed_source)

    # Collapse every project identifier (originals in the original prompt, their
    # ``idNNNN`` targets in the renamed prompt) to a single sentinel. If the
    # rename touched only identifier positions, the two masked texts are equal.
    def mask_originals(text: str) -> str:
        return _IDENT_TOKEN_RE.sub("ID", text)

    def mask_targets(text: str) -> str:
        # ``\bid\d{4}`` with a ``(?!\d)`` tail so a target glued to a CamelCase
        # suffix (``id0000CallExample``) is recognized, matching the identifier
        # regex's glue tolerance.
        return re.sub(r"\bid\d{4}(?!\d)", "ID", text)

    masked_original = mask_originals(original_prompt)
    masked_renamed = mask_targets(prompt_from_renamed_inputs)

    assert masked_original == masked_renamed


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
