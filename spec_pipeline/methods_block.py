"""Methods_Block_Generator (Requirement 17).

Deterministic construction of a CVL ``methods`` block (plus ``using`` alias
declarations and CVL type declarations) from a Stage 1 table.

This module is the corrected replacement for
``spec_pipeline.stage3_rules._generate_methods_block`` and fixes its three
defects:

1. Public state-variable getters read the declared type from the state
   variable's ``type`` field (the real field name) rather than the
   nonexistent ``type_name`` (R17.1), and that computed CVL return type is
   emitted in the getter signature (R17.2).
2. The dedupe key is ``(contract_name, function_name, param_types)`` so two
   contracts declaring the same function name each keep an entry; there is no
   global ``seen`` set (R17.3).
3. Types with no CVL expression are *omitted* from the methods block and
   recorded in an exclusion report (reason ``unsupported_type``) instead of
   being silently coerced to ``uint256`` (R17.6, R17.12).

Design note on imports: this module intentionally does NOT import
``spec_pipeline.stage1_extract`` (which pulls in ``solidity_graph.analyzer`` ->
slither). It operates on the Stage 1 table purely by attribute access
(duck typing): ``table.contracts`` maps contract name -> contract, and each
contract exposes ``function_gates`` and ``state_vars`` whose elements carry the
documented fields. The small ``_extract_*_types`` regex helpers are copied here
so the module has no slither-tainted import chain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# CVL type vocabulary
# ---------------------------------------------------------------------------

# Primitive value types CVL understands natively.
_PRIMITIVE_TYPES = {
    "address",
    "bool",
    "string",
    "bytes",
    # sized bytes
    *(f"bytes{n}" for n in range(1, 33)),
    # sized unsigned ints (multiples of 8)
    *(f"uint{n}" for n in range(8, 257, 8)),
    "uint",
    # sized signed ints (multiples of 8)
    *(f"int{n}" for n in range(8, 257, 8)),
    "int",
}


def _extract_struct_types(source_code: str) -> set[str]:
    """Return every ``struct`` type name declared in *source_code*."""
    return {
        m.group(1)
        for m in re.finditer(r"\bstruct\s+(\w+)\s*\{", source_code, re.MULTILINE)
    }


def _extract_enum_types(source_code: str) -> set[str]:
    """Return every ``enum`` type name declared in *source_code*."""
    return {
        m.group(1)
        for m in re.finditer(r"\benum\s+(\w+)\s*\{", source_code, re.MULTILINE)
    }


def _extract_user_defined_types(source_code: str) -> set[str]:
    """Return the union of struct and enum type names in *source_code*."""
    if not source_code:
        return set()
    types: set[str] = set()
    types.update(_extract_struct_types(source_code))
    types.update(_extract_enum_types(source_code))
    return types


# ---------------------------------------------------------------------------
# Result / report data models
# ---------------------------------------------------------------------------


@dataclass
class ExclusionEntry:
    """One function/getter omitted from the methods block.

    Fields mirror the exclusion-report shape required by R17.6/R17.12:
    which contract, the function or variable name, the reason code, and the
    offending type text that could not be expressed in CVL.
    """

    contract: str
    name: str
    reason: str
    type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "contract": self.contract,
            "name": self.name,
            "reason": self.reason,
            "type": self.type,
        }


@dataclass
class MethodsBlockResult:
    """Return value of :func:`generate_methods_block`.

    ``text``            the full methods-block text (using decls + CVL type
                        declarations + the ``methods { ... }`` block).
    ``exclusion_report`` list of :class:`ExclusionEntry` for omitted gates.
    ``entries``         the ordered list of emitted :class:`MethodEntry`.
    """

    text: str
    exclusion_report: list[ExclusionEntry] = field(default_factory=list)
    entries: list["MethodEntry"] = field(default_factory=list)

    def __str__(self) -> str:  # convenience for callers wanting the string
        return self.text

    @property
    def exclusions(self) -> list[dict[str, str]]:
        """Exclusion report as a list of plain dicts."""
        return [e.as_dict() for e in self.exclusion_report]


@dataclass
class MethodEntry:
    """One emitted methods-block entry (a gate or a public state-var getter)."""

    contract: str
    name: str
    param_types: tuple[str, ...]
    return_type: str  # "" when there is no return value
    envfree: bool
    is_getter: bool

    @property
    def dedupe_key(self) -> tuple[str, str, tuple[str, ...]]:
        # (contract, function, ordered param types) — R17.3
        return (self.contract, self.name, self.param_types)


# ---------------------------------------------------------------------------
# Type resolution
# ---------------------------------------------------------------------------


def _normalize_type_token(text: str) -> str:
    """Trim location/qualifier keywords from a raw Solidity type token."""
    text = text.strip()
    # Drop trailing storage-location / qualifier keywords if a name slipped in.
    for kw in (" memory", " calldata", " storage", " payable"):
        if text.endswith(kw):
            text = text[: -len(kw)].strip()
    return text


def _cvl_type(
    raw: str,
    user_defined: set[str],
    expressible: set[str] | None = None,
    contract_types: set[str] | None = None,
) -> str | None:
    """Map a single Solidity type token to its CVL type.

    Returns the CVL type string, or ``None`` when the type has no CVL
    expression. A user-defined struct/enum has no CVL expression *unless* it is
    listed in *expressible* (meaning a CVL type declaration is being emitted for
    it), in which case the type name itself is the CVL type. A token that names
    a contract or interface in the table (listed in *contract_types*) is a
    reference type and resolves to CVL ``address``. Arrays of a CVL-expressible
    element remain arrays (e.g. ``uint256[]``, ``address[]`` for a
    contract-typed array); ``bytesNN`` and ``bytes``/``string`` are primitives.
    """
    expressible = expressible or set()
    contract_types = contract_types or set()
    t = _normalize_type_token(raw)
    if not t:
        return None

    # Array types: element[] / element[N]. Resolve the element type; if the
    # element is CVL-expressible the array is too.
    array_match = re.match(r"^(.*?)(\s*\[[^\]]*\])+$", t)
    if array_match and t.endswith("]"):
        # Split off the (possibly multi-dimensional) suffix.
        base = re.sub(r"(\s*\[[^\]]*\])+$", "", t).strip()
        suffix = t[len(base):].replace(" ", "")
        base_cvl = _cvl_type(base, user_defined, expressible, contract_types)
        if base_cvl is None:
            return None
        return f"{base_cvl}{suffix}"

    # A user-defined struct/enum with an emitted CVL declaration is expressible
    # by its own name.
    if t in expressible:
        return t

    # user-defined struct/enum without a declaration has no CVL expression.
    if t in user_defined:
        return None

    if t in _PRIMITIVE_TYPES:
        return t

    # A contract/interface reference is an address in CVL.
    if t in contract_types:
        return "address"

    # Unknown / unsupported type -> no CVL expression (do NOT coerce).
    return None


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split *text* on *sep* ignoring separators nested inside () / <> / []."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    openers = "(<["
    closers = ")>]"
    for ch in text:
        if ch in openers:
            depth += 1
            current.append(ch)
        elif ch in closers:
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _parse_param_types(signature: str) -> list[str]:
    """Extract the ordered list of raw parameter type tokens from a signature.

    ``signature`` looks like ``"(uint256 poolId_, address addr) -> bool"``.
    Each parameter is ``"<type> [name]"``; we keep the type (everything but the
    trailing identifier when a name is present).
    """
    params_raw = signature.split("->")[0].strip()
    params_raw = params_raw.strip()
    if params_raw.startswith("("):
        # Strip the outermost parentheses only.
        params_raw = params_raw[1:]
        if params_raw.endswith(")"):
            params_raw = params_raw[:-1]
    params_raw = params_raw.strip()
    if not params_raw:
        return []

    types: list[str] = []
    for part in _split_top_level(params_raw):
        part = part.strip()
        if not part:
            continue
        # A parameter is "<type tokens...> <name>" or just "<type>". The type
        # can itself contain spaces only via location keywords, which
        # _normalize_type_token strips. Take everything but a trailing
        # identifier if there is more than one token.
        tokens = part.split()
        if len(tokens) >= 2:
            # Drop a trailing name; but keep location keyword handling to
            # _normalize_type_token. If the last token is a location keyword,
            # there is no name, keep all tokens joined.
            if tokens[-1] in ("memory", "calldata", "storage", "payable"):
                type_text = " ".join(tokens)
            else:
                type_text = " ".join(tokens[:-1])
        else:
            type_text = tokens[0]
        types.append(_normalize_type_token(type_text))
    return types


def _resolve_getter(
    sv: Any,
    user_defined: set[str],
    expressible: set[str] | None = None,
    contract_types: set[str] | None = None,
) -> tuple[list[str], str | None, str]:
    """Resolve a public state variable getter into (param_types, return_type, raw).

    Handles mapping and array getters (R17.11): each mapping key level and each
    array index level becomes one parameter, and the innermost value type is the
    return type. ``return_type`` is ``None`` when the innermost value type has no
    CVL expression. ``raw`` is the offending raw type text for the exclusion
    report.
    """
    raw = _normalize_type_token(getattr(sv, "type", "") or "")
    params: list[str] = []
    current = raw

    # Peel nested mapping(K => V) and trailing array dimensions.
    while True:
        current = current.strip()
        mapping_match = re.match(r"^mapping\s*\((.*)\)$", current, re.DOTALL)
        if mapping_match:
            inner = mapping_match.group(1)
            # Split on the FIRST top-level "=>".
            key_text, value_text = _split_mapping(inner)
            key_cvl = _cvl_type(key_text, user_defined, expressible, contract_types)
            if key_cvl is None:
                return params, None, key_text.strip()
            params.append(key_cvl)
            current = value_text.strip()
            continue

        # Array dimension on the value type: element[] -> index param.
        if current.endswith("]"):
            base = re.sub(r"(\s*\[[^\]]*\])+$", "", current).strip()
            # number of dimensions
            dims = re.findall(r"\[[^\]]*\]", current[len(base):])
            for _ in dims:
                params.append("uint256")  # array index
            current = base
            continue
        break

    return_type = _cvl_type(current, user_defined, expressible, contract_types)
    return params, return_type, current


def _split_mapping(inner: str) -> tuple[str, str]:
    """Split ``K => V`` at the first top-level ``=>``."""
    depth = 0
    i = 0
    while i < len(inner) - 1:
        ch = inner[i]
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth = max(0, depth - 1)
        elif depth == 0 and inner[i : i + 2] == "=>":
            return inner[:i], inner[i + 2 :]
        i += 1
    # No arrow found; treat whole thing as value with no key.
    return "", inner


# ---------------------------------------------------------------------------
# Alias generation for multi-contract projects
# ---------------------------------------------------------------------------


def _make_alias(contract_name: str, used: set[str]) -> str:
    """Derive a unique CVL alias identifier for *contract_name*."""
    # Base: the contract name itself is a valid identifier in practice; ensure
    # uniqueness against already-used aliases (and reserved unqualified use).
    base = contract_name if contract_name else "C"
    alias = base
    n = 2
    while alias in used:
        alias = f"{base}{n}"
        n += 1
    used.add(alias)
    return alias


# ---------------------------------------------------------------------------
# CVL type declarations for CVL-expressible structs/enums (R17.5)
# ---------------------------------------------------------------------------


def _extract_struct_fields(source_code: str, struct_name: str) -> list[tuple[str, str]] | None:
    """Return [(field_type, field_name), ...] for ``struct struct_name``.

    Returns ``None`` when the struct body cannot be located.
    """
    m = re.search(
        r"\bstruct\s+" + re.escape(struct_name) + r"\s*\{(.*?)\}",
        source_code,
        re.DOTALL,
    )
    if not m:
        return None
    body = m.group(1)
    fields: list[tuple[str, str]] = []
    for stmt in body.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        tokens = stmt.split()
        if len(tokens) < 2:
            return None
        ftype = " ".join(tokens[:-1])
        fname = tokens[-1]
        fields.append((_normalize_type_token(ftype), fname))
    return fields


def _cvl_struct_declaration(
    struct_name: str,
    source_code: str,
    user_defined: set[str],
    expressible: set[str] | None = None,
) -> str | None:
    """Emit a CVL ``struct`` type declaration when every field is CVL-expressible.

    A field whose type is itself a CVL-expressible struct/enum (listed in
    *expressible*) is allowed; a field with any non-CVL type makes the whole
    struct inexpressible (returns ``None``).
    """
    fields = _extract_struct_fields(source_code, struct_name)
    if not fields:
        return None
    lines = [f"struct {struct_name} {{"]
    for ftype, fname in fields:
        cvl = _cvl_type(ftype, user_defined, expressible)
        if cvl is None:
            return None
        lines.append(f"    {cvl} {fname};")
    lines.append("}")
    return "\n".join(lines)


def _compute_expressible_types(
    source_code: str,
) -> tuple[set[str], dict[str, str]]:
    """Determine which structs/enums are CVL-expressible and their declarations.

    Enums are always CVL-expressible (they map to a CVL ``uint8``-like sort; we
    emit them as-is). Structs are expressible only when every field type is
    itself CVL-expressible; this is computed to a fixed point so a struct of
    CVL-expressible structs also qualifies. Returns the set of expressible type
    names and a mapping name -> CVL declaration text.
    """
    if not source_code:
        return set(), {}

    struct_names = _extract_struct_types(source_code)
    enum_names = _extract_enum_types(source_code)
    user_defined = struct_names | enum_names

    # Enums are expressible; emit a simple declaration.
    expressible: set[str] = set(enum_names)
    decls: dict[str, str] = {}
    for ename in enum_names:
        members = _extract_enum_members(source_code, ename)
        if members:
            decls[ename] = "enum " + ename + " {" + ", ".join(members) + "}"
        else:
            decls[ename] = f"enum {ename} {{}}"

    # Fixed-point over structs.
    changed = True
    while changed:
        changed = False
        for sname in sorted(struct_names):
            if sname in expressible:
                continue
            decl = _cvl_struct_declaration(sname, source_code, user_defined, expressible)
            if decl is not None:
                expressible.add(sname)
                decls[sname] = decl
                changed = True

    # Prune declarations for types that never became expressible.
    decls = {k: v for k, v in decls.items() if k in expressible}
    return expressible, decls


def _extract_enum_members(source_code: str, enum_name: str) -> list[str]:
    """Return the member identifiers of ``enum enum_name`` (may be empty)."""
    m = re.search(
        r"\benum\s+" + re.escape(enum_name) + r"\s*\{(.*?)\}",
        source_code,
        re.DOTALL,
    )
    if not m:
        return []
    body = m.group(1)
    return [tok.strip() for tok in body.split(",") if tok.strip()]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_methods_block(table: Any, source_code: str = "") -> MethodsBlockResult:
    """Generate the CVL methods block (+ using/type declarations) for *table*.

    See module docstring for the defects fixed relative to the old
    ``_generate_methods_block``. Operates on *table* by attribute access so it
    carries no slither-tainted import.

    Returns a :class:`MethodsBlockResult` whose ``.text`` is the emitted text
    and whose ``.exclusion_report`` lists omitted gates (R17.6/R17.12).
    """
    user_defined = _extract_user_defined_types(source_code)
    # CVL-expressible structs/enums and their declarations (R17.5). Only types
    # actually referenced by an emitted entry are added to `type_decls` below.
    expressible, expressible_decls = _compute_expressible_types(source_code)

    contracts: dict[str, Any] = getattr(table, "contracts", {}) or {}
    contract_names = sorted(contracts.keys())
    # Contract/interface names in the table are reference types; in CVL a
    # reference to a contract/interface is an `address`. Interfaces appearing
    # in the table as contracts (kind == "interface") are already keys of
    # `contracts`, so this set covers them.
    contract_types: set[str] = set(contract_names)

    # Multi-contract: one contract is unqualified, the rest get `using Alias`.
    alias_by_contract: dict[str, str] = {}
    using_decls: list[tuple[str, str]] = []  # (contract, alias) in emit order
    if len(contract_names) > 1:
        used_aliases: set[str] = set()
        # Designate the first contract (sorted) as the unqualified one.
        unqualified = contract_names[0]
        for cname in contract_names[1:]:
            alias = _make_alias(cname, used_aliases)
            alias_by_contract[cname] = alias
            using_decls.append((cname, alias))

    entries: list[MethodEntry] = []
    exclusions: list[ExclusionEntry] = []
    # Per-contract dedupe: key includes the contract so shared names survive.
    seen: set[tuple[str, str, tuple[str, ...]]] = set()

    # Track distinct CVL-expressible struct/enum type declarations to emit.
    type_decls: dict[str, str] = {}

    for cname in contract_names:
        contract = contracts[cname]

        # --- function gates ---
        for fg in getattr(contract, "function_gates", []) or []:
            if getattr(fg, "is_constructor", False) or getattr(
                fg, "is_fallback", False
            ) or getattr(fg, "is_receive", False):
                continue

            signature = getattr(fg, "signature", "") or ""
            raw_param_types = _parse_param_types(signature)

            # Resolve each parameter type into CVL, treating CVL-expressible
            # structs/enums as valid (their declarations are emitted). On the
            # first type with no CVL expression, exclude the whole entry with a
            # single exclusion record naming the offending type (R17.6/R17.12).
            cvl_params: list[str] = []
            referenced_types: list[str] = []
            offending: str | None = None
            for raw in raw_param_types:
                cvl = _cvl_type(raw, user_defined, expressible, contract_types)
                if cvl is None:
                    offending = raw
                    break
                cvl_params.append(cvl)
                referenced_types.append(cvl)

            # Return type of the gate: parse the "-> T" segment if present.
            # A multi-value return ("-> A, B, C") is expressed in CVL as
            # ``returns (A, B, C)``; each component is resolved independently.
            return_cvl = ""
            if offending is None and "->" in signature:
                ret_raw = signature.split("->", 1)[1].strip()
                if ret_raw:
                    ret_components = [
                        comp.strip()
                        for comp in _split_top_level(ret_raw)
                        if comp.strip()
                    ]
                    resolved_components: list[str] = []
                    for comp in ret_components:
                        rc = _cvl_type(comp, user_defined, expressible, contract_types)
                        if rc is None:
                            offending = comp
                            break
                        resolved_components.append(rc)
                    if offending is None and resolved_components:
                        return_cvl = ", ".join(resolved_components)
                        referenced_types.extend(resolved_components)

            if offending is not None:
                exclusions.append(
                    ExclusionEntry(cname, fg.name, "unsupported_type", offending)
                )
                continue

            # Record CVL type declarations for any expressible struct/enum this
            # entry references (strip array suffixes to get the base type name).
            for rt in referenced_types:
                base = re.sub(r"(\s*\[[^\]]*\])+$", "", rt).strip()
                if base in expressible_decls:
                    type_decls[base] = expressible_decls[base]

            key = (cname, fg.name, tuple(cvl_params))
            if key in seen:
                continue
            seen.add(key)
            envfree = getattr(fg, "mutability", "") in ("view", "pure")
            entries.append(
                MethodEntry(
                    contract=cname,
                    name=fg.name,
                    param_types=tuple(cvl_params),
                    return_type=return_cvl,
                    envfree=envfree,
                    is_getter=False,
                )
            )

        # --- public state variable getters ---
        for sv in getattr(contract, "state_vars", []) or []:
            if getattr(sv, "visibility", "") != "public":
                continue
            params, return_type, offender = _resolve_getter(
                sv, user_defined, expressible, contract_types
            )
            if return_type is None:
                exclusions.append(
                    ExclusionEntry(cname, sv.name, "unsupported_type", offender)
                )
                continue
            base_ret = re.sub(r"(\s*\[[^\]]*\])+$", "", return_type).strip()
            if base_ret in expressible_decls:
                type_decls[base_ret] = expressible_decls[base_ret]
            key = (cname, sv.name, tuple(params))
            if key in seen:
                continue
            seen.add(key)
            # Every public state-variable getter is envfree (R17.7).
            entries.append(
                MethodEntry(
                    contract=cname,
                    name=sv.name,
                    param_types=tuple(params),
                    return_type=return_type,
                    envfree=True,
                    is_getter=True,
                )
            )

    # Deterministic ordering: contract, then function name, then param types (R17.8).
    entries.sort(key=lambda e: (e.contract, e.name, e.param_types))
    exclusions.sort(key=lambda x: (x.contract, x.name, x.type))

    text = _render(entries, using_decls, type_decls, alias_by_contract)
    return MethodsBlockResult(text=text, exclusion_report=exclusions, entries=entries)


def _render(
    entries: list[MethodEntry],
    using_decls: list[tuple[str, str]],
    type_decls: dict[str, str],
    alias_by_contract: dict[str, str],
) -> str:
    """Render the full deterministic text block."""
    out: list[str] = []

    # `using Alias` per auxiliary contract (R17.4), ordered by contract name.
    for cname, alias in sorted(using_decls, key=lambda t: t[0]):
        out.append(f"using {cname} as {alias};")
    if using_decls:
        out.append("")

    # CVL type declarations for CVL-expressible structs/enums (R17.5),
    # ordered by type name for determinism.
    for tname in sorted(type_decls):
        out.append(type_decls[tname])
        out.append("")

    out.append("methods {")
    last_contract: str | None = None
    for e in entries:
        if e.contract != last_contract:
            out.append(f"    // {e.contract}")
            last_contract = e.contract
        params = ", ".join(e.param_types)
        ret = f" returns ({e.return_type})" if e.return_type else ""
        envfree = " envfree" if e.envfree else ""
        alias = alias_by_contract.get(e.contract)
        target = f"{alias}." if alias else ""
        out.append(
            f"    function {target}{e.name}({params}) external{ret}{envfree};"
        )
    out.append("}")
    return "\n".join(out)
