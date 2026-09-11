"""
Context_Budgeter (Requirement 16)
=================================

Selects and packs Solidity source content into a prompt within a configured
character budget, WITHOUT silently truncating. Whenever content is dropped the
budgeter records exactly what was omitted (file paths, character count,
substitution flags) so the Run_Manifest can report it and the prompt can name
the omitted contracts (R16.4).

Design constraints
------------------
* **Import-safe without slither.** This module is pure text/data. It operates on
  duck-typed table/graph inputs and NEVER imports ``stage1_extract`` or
  ``solidity_graph.analyzer``. The Stage 1 objects it reads (contracts, function
  gates, caller edges) are accessed by attribute with ``getattr`` fallbacks, so
  any object exposing the same shape works.
* **Cut at ``.sol`` file boundaries.** A file is either included whole or omitted
  whole; the budgeter stops at the first file that would exceed the budget
  (R16.3). It never emits a partially-truncated file.
* **The contract under analysis comes first and in full when it fits** (R16.2,
  R16.5). If its own source exceeds the budget alone, the budgeter substitutes a
  compact representation (Stage 1 table entry + Gated_Function bodies, then
  signatures) and records the substitution (R16.6, R16.11).

Budget resolution (R16.1, R16.10)
---------------------------------
``LLM_MAX_INPUT_CHARS`` sets the character budget; default 200000. A value that
is not a positive integer is rejected: the default is used and the rejected raw
value is recorded on the result so the Run_Manifest can surface it.

Public interface
----------------
* ``resolve_budget(env=None) -> BudgetResolution``
* ``Context_Budgeter().pack_source(...) -> PackResult``
* ``merge_per_contract_specs(...) -> MergeResult``  (R16.7, R16.8)

The ``PackResult`` carries the packed text plus an ``OmissionRecord``. When
nothing is omitted the record is empty (``omitted_paths == []``,
``omitted_char_count == 0``) so callers can treat "no omission" uniformly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

DEFAULT_MAX_INPUT_CHARS = 200000
_ENV_KEY = "LLM_MAX_INPUT_CHARS"


# ---------------------------------------------------------------------------
# Result / record types
# ---------------------------------------------------------------------------


@dataclass
class BudgetResolution:
    """The resolved character budget and, when applicable, the rejected value."""

    budget: int
    rejected_value: Optional[str] = None  # raw env value that was not a positive int


@dataclass
class OmissionRecord:
    """What the budgeter dropped, for the Run_Manifest (R16.4, R16.6, R16.11).

    An "empty" record (nothing omitted) has ``omitted_paths == []`` and
    ``omitted_char_count == 0`` with both substitution flags ``False``.
    """

    omitted_paths: list[str] = field(default_factory=list)
    omitted_char_count: int = 0
    # R16.6: the analyzed contract source was replaced by its table entry + the
    # bodies of its Gated_Functions.
    substituted_contract_body: bool = False
    # R16.11: even the entry + bodies exceeded budget, so signatures were used.
    substituted_signatures_only: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            not self.omitted_paths
            and self.omitted_char_count == 0
            and not self.substituted_contract_body
            and not self.substituted_signatures_only
        )

    def to_manifest(self) -> dict:
        """Serialize for the Run_Manifest."""
        return {
            "omitted_paths": list(self.omitted_paths),
            "omitted_char_count": self.omitted_char_count,
            "substituted_contract_body": self.substituted_contract_body,
            "substituted_signatures_only": self.substituted_signatures_only,
        }


@dataclass
class PackResult:
    """The packed prompt source plus its omission record."""

    text: str
    omission: OmissionRecord = field(default_factory=OmissionRecord)
    # Contract names to name in the prompt as omitted (R16.4). May be empty.
    omitted_contract_names: list[str] = field(default_factory=list)
    budget: int = DEFAULT_MAX_INPUT_CHARS
    rejected_budget_value: Optional[str] = None


@dataclass
class SourceFile:
    """One ``.sol`` source unit the budgeter may include or omit whole."""

    path: str  # relative path, used for tie-break ordering
    content: str
    contracts: tuple[str, ...] = ()  # contract names declared in this file


@dataclass
class RuleRename:
    """One recorded rule-name collision resolution (R16.8)."""

    original: str
    renamed: str
    contract: str


@dataclass
class MergeResult:
    """Merged spec text plus the rename log (R16.7, R16.8)."""

    spec_text: str
    renames: list[RuleRename] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Budget resolution (R16.1, R16.10)
# ---------------------------------------------------------------------------


def resolve_budget(env: Optional[Mapping[str, str]] = None) -> BudgetResolution:
    """Resolve the character budget from ``LLM_MAX_INPUT_CHARS``.

    Default 200000. If the configured value is not a positive integer, use the
    default AND record the rejected raw value (R16.1, R16.10).
    """
    source = os.environ if env is None else env
    raw = source.get(_ENV_KEY)
    if raw is None:
        return BudgetResolution(budget=DEFAULT_MAX_INPUT_CHARS)

    text = raw.strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        return BudgetResolution(budget=DEFAULT_MAX_INPUT_CHARS, rejected_value=raw)
    if value <= 0:
        return BudgetResolution(budget=DEFAULT_MAX_INPUT_CHARS, rejected_value=raw)
    return BudgetResolution(budget=value)


# ---------------------------------------------------------------------------
# Stage 1 table introspection helpers (duck-typed; slither-free)
# ---------------------------------------------------------------------------


def _iter_contract_entries(table: Any) -> dict[str, Any]:
    """Return a {contract_name: entry} mapping from a duck-typed Stage 1 table."""
    if table is None:
        return {}
    contracts = getattr(table, "contracts", None)
    if contracts is None and isinstance(table, Mapping):
        contracts = table.get("contracts")
    if isinstance(contracts, Mapping):
        return dict(contracts)
    return {}


def _entry_source_file(entry: Any) -> str:
    return getattr(entry, "source_file", "") or ""


def _gate_is_special(gate: Any) -> bool:
    return any(
        getattr(gate, attr, False)
        for attr in ("is_constructor", "is_fallback", "is_receive")
    )


def _gate_is_gated(gate: Any) -> bool:
    """A Gated_Function carries an access-control modifier (R16.6)."""
    mod = getattr(gate, "modifier", "none") or "none"
    return mod != "none" and not _gate_is_special(gate)


# ---------------------------------------------------------------------------
# Caller-edge reachability (R16.2)
# ---------------------------------------------------------------------------


def _caller_hop_distances(table: Any, root_contract: str) -> dict[str, int]:
    """BFS hop distance from ``root_contract`` over first-party caller edges.

    Uses each contract entry's ``caller_edges`` (caller_contract ->
    callee_contract). Returns {contract_name: hop_distance} for every reachable
    contract EXCLUDING the root itself. Distance 1 = directly called by the root.
    """
    entries = _iter_contract_entries(table)
    if not entries:
        return {}

    # Build adjacency: caller -> set(callee) across all first-party edges.
    adjacency: dict[str, set[str]] = {}
    for _cname, entry in entries.items():
        for edge in getattr(entry, "caller_edges", []) or []:
            caller = getattr(edge, "caller_contract", "")
            callee = getattr(edge, "callee_contract", "")
            if not caller or not callee or caller == callee:
                continue
            adjacency.setdefault(caller, set()).add(callee)

    distances: dict[str, int] = {}
    frontier = {root_contract}
    hop = 0
    visited = {root_contract}
    while frontier:
        hop += 1
        nxt: set[str] = set()
        for node in frontier:
            for callee in adjacency.get(node, ()):  # noqa: B007
                if callee in visited:
                    continue
                visited.add(callee)
                distances[callee] = hop
                nxt.add(callee)
        frontier = nxt
    return distances


def _priority_key(
    file: SourceFile,
    analyzed_file: Optional[str],
    contract_distance: Mapping[str, int],
) -> tuple[int, int, str]:
    """Sort key: (priority_tier, hop_distance, relative_path) (R16.2).

    Tier 0: the file holding the contract under analysis.
    Tier 1: files holding contracts reachable via caller edges (ascending hop).
    Tier 2: the rest.
    Equal-priority files are ordered by ascending relative path.
    """
    if analyzed_file is not None and file.path == analyzed_file:
        return (0, 0, file.path)
    # Nearest reachable contract declared in this file, if any.
    best_hop: Optional[int] = None
    for cname in file.contracts:
        hop = contract_distance.get(cname)
        if hop is not None and (best_hop is None or hop < best_hop):
            best_hop = hop
    if best_hop is not None:
        return (1, best_hop, file.path)
    return (2, 0, file.path)


# ---------------------------------------------------------------------------
# Compact substitution for an oversize analyzed contract (R16.6, R16.11)
# ---------------------------------------------------------------------------


def _extract_function_body(source: str, fn_name: str) -> Optional[str]:
    """Best-effort brace-matched extraction of ``fn_name``'s body from source.

    Returns the ``function <name>(...) { ... }`` block, or ``None`` if not found.
    Pure text; tolerant of formatting.
    """
    pattern = re.compile(r"function\s+" + re.escape(fn_name) + r"\s*\(")
    m = pattern.search(source)
    if not m:
        return None
    # Find the opening brace after the signature.
    brace_start = source.find("{", m.end())
    if brace_start == -1:
        return None
    depth = 0
    i = brace_start
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[m.start() : i + 1]
        i += 1
    return None


def _gate_signature_line(entry_name: str, gate: Any) -> str:
    name = getattr(gate, "name", "")
    signature = getattr(gate, "signature", "") or "()"
    modifier = getattr(gate, "modifier", "none") or "none"
    return f"    function {name}{signature}  // gate: {modifier}"


def _substitute_contract(
    entry: Any,
    table_entry_text: str,
    analyzed_source: str,
    budget: int,
) -> tuple[str, bool, bool]:
    """Build a compact substitute for an oversize analyzed contract.

    Returns ``(text, substituted_body, substituted_signatures_only)``.

    First tries the Stage 1 table entry + Gated_Function bodies (R16.6). If that
    still exceeds the budget, falls back to the entry + Gated_Function signatures
    (R16.11).
    """
    name = getattr(entry, "name", "") or "Contract"
    gates = list(getattr(entry, "function_gates", []) or [])
    gated = [g for g in gates if _gate_is_gated(g)]

    header = table_entry_text or f"=== Stage 1 entry for {name} ==="

    # Attempt 1: entry + bodies of Gated_Functions (R16.6).
    body_parts: list[str] = []
    for g in gated:
        gname = getattr(g, "name", "")
        body = _extract_function_body(analyzed_source, gname) if gname else None
        if body:
            body_parts.append(body)
    with_bodies = (
        f"{header}\n"
        f"// NOTE: {name} source exceeded the character budget; showing the "
        f"Stage 1 entry plus the bodies of its access-gated functions.\n"
        "```solidity\n" + "\n\n".join(body_parts) + "\n```\n"
    )
    if len(with_bodies) <= budget:
        return with_bodies, True, False

    # Attempt 2: entry + Gated_Function signatures only (R16.11).
    sig_lines = [_gate_signature_line(name, g) for g in gated]
    with_sigs = (
        f"{header}\n"
        f"// NOTE: {name} source and gated-function bodies exceeded the budget; "
        f"showing the Stage 1 entry plus gated-function signatures only.\n"
        "```solidity\n" + "\n".join(sig_lines) + "\n```\n"
    )
    return with_sigs, True, True


# ---------------------------------------------------------------------------
# Context_Budgeter
# ---------------------------------------------------------------------------


class Context_Budgeter:
    """Packs source content into a prompt within a configured character budget."""

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self._resolution = resolve_budget(env)

    @property
    def budget(self) -> int:
        return self._resolution.budget

    @property
    def rejected_value(self) -> Optional[str]:
        return self._resolution.rejected_value

    # -- structured packing (R16.2, R16.3, R16.5) --------------------------

    def pack_source(
        self,
        files: Optional[Iterable[SourceFile]] = None,
        *,
        analyzed_contract: Optional[str] = None,
        table: Any = None,
        table_entry_text: str = "",
        raw_source: Optional[str] = None,
        separator: str = "\n\n",
    ) -> PackResult:
        """Select and pack source content within the budget.

        Two modes:

        * **Structured** — ``files`` is provided (each a :class:`SourceFile`).
          Priority selection + boundary cutting apply (R16.2, R16.3). When
          ``analyzed_contract``/``table`` are given, the contract under analysis
          is placed first and reachable contracts are ordered by hop distance.
        * **Raw fallback** — only ``raw_source`` is provided (no per-file split).
          A single-file budget cut applies: include it whole when it fits, else
          record it as an omission (never silently truncate).

        Always records omissions on the returned :class:`PackResult`.
        """
        budget = self.budget
        rejected = self.rejected_value

        file_list = list(files) if files is not None else []

        # Raw-source fallback (R16.9 note): no table/graph, only concatenated text.
        if not file_list:
            return self._pack_raw(raw_source or "", budget, rejected)

        # Locate the analyzed contract's file, if identifiable.
        analyzed_file = self._locate_analyzed_file(
            file_list, analyzed_contract, table
        )

        # Special case: the analyzed contract's own source exceeds the budget
        # alone -> substitute (R16.6, R16.11) and omit everything else.
        if analyzed_file is not None:
            af = next(f for f in file_list if f.path == analyzed_file)
            if len(af.content) > budget:
                return self._pack_substituted(
                    af, file_list, analyzed_contract, table, table_entry_text,
                    budget, rejected,
                )

        # Priority ordering (R16.2).
        distances = (
            _caller_hop_distances(table, analyzed_contract)
            if (table is not None and analyzed_contract)
            else {}
        )
        ordered = sorted(
            file_list,
            key=lambda f: _priority_key(f, analyzed_file, distances),
        )

        included: list[SourceFile] = []
        omitted: list[SourceFile] = []
        used = 0
        sep_len = len(separator)
        stopped = False
        for f in ordered:
            if stopped:
                omitted.append(f)
                continue
            addition = len(f.content) + (sep_len if included else 0)
            if used + addition <= budget:
                included.append(f)
                used += addition
            else:
                # Cut at this file boundary; include/omit whole (R16.3).
                omitted.append(f)
                stopped = True

        text = separator.join(f.content for f in included)
        omission = OmissionRecord(
            omitted_paths=sorted(f.path for f in omitted),
            omitted_char_count=sum(len(f.content) for f in omitted),
        )
        omitted_names = self._omitted_contract_names(omitted)
        return PackResult(
            text=text,
            omission=omission,
            omitted_contract_names=omitted_names,
            budget=budget,
            rejected_budget_value=rejected,
        )

    # -- helpers -----------------------------------------------------------

    def _pack_raw(
        self, raw_source: str, budget: int, rejected: Optional[str]
    ) -> PackResult:
        """Single-file budget cut for raw concatenated source (R16.9 fallback)."""
        if len(raw_source) <= budget:
            return PackResult(
                text=raw_source,
                omission=OmissionRecord(),
                omitted_contract_names=[],
                budget=budget,
                rejected_budget_value=rejected,
            )
        # Too big and unsplittable: omit whole, record the omission. Never
        # silently truncate.
        return PackResult(
            text="",
            omission=OmissionRecord(
                omitted_paths=["<raw_source>"],
                omitted_char_count=len(raw_source),
            ),
            omitted_contract_names=[],
            budget=budget,
            rejected_budget_value=rejected,
        )

    def _pack_substituted(
        self,
        analyzed_file: SourceFile,
        file_list: list[SourceFile],
        analyzed_contract: Optional[str],
        table: Any,
        table_entry_text: str,
        budget: int,
        rejected: Optional[str],
    ) -> PackResult:
        entries = _iter_contract_entries(table)
        entry = entries.get(analyzed_contract) if analyzed_contract else None
        sub_text, did_body, sigs_only = _substitute_contract(
            entry, table_entry_text, analyzed_file.content, budget
        )
        others = [f for f in file_list if f.path != analyzed_file.path]
        omission = OmissionRecord(
            omitted_paths=sorted(f.path for f in others) + [analyzed_file.path],
            omitted_char_count=len(analyzed_file.content)
            + sum(len(f.content) for f in others),
            substituted_contract_body=did_body,
            substituted_signatures_only=sigs_only,
        )
        return PackResult(
            text=sub_text,
            omission=omission,
            omitted_contract_names=self._omitted_contract_names(others),
            budget=budget,
            rejected_budget_value=rejected,
        )

    @staticmethod
    def _locate_analyzed_file(
        file_list: list[SourceFile],
        analyzed_contract: Optional[str],
        table: Any,
    ) -> Optional[str]:
        if not analyzed_contract:
            return None
        # Prefer a file that declares the contract by name.
        for f in file_list:
            if analyzed_contract in f.contracts:
                return f.path
        # Fall back to the Stage 1 table's recorded source_file.
        entries = _iter_contract_entries(table)
        entry = entries.get(analyzed_contract)
        if entry is not None:
            src = _entry_source_file(entry)
            for f in file_list:
                if src and (f.path == src or f.path.endswith(src) or src.endswith(f.path)):
                    return f.path
        return None

    @staticmethod
    def _omitted_contract_names(omitted: list[SourceFile]) -> list[str]:
        names: list[str] = []
        for f in omitted:
            for c in f.contracts:
                if c and c not in names:
                    names.append(c)
        return sorted(names)


# ---------------------------------------------------------------------------
# Rule_Writer merge helper (R16.7, R16.8)
# ---------------------------------------------------------------------------

_METHODS_BLOCK_RE = re.compile(r"methods\s*\{.*?\}", re.DOTALL)
_RULE_DECL_RE = re.compile(r"\brule\s+([A-Za-z_]\w*)")


def _strip_methods_blocks(spec_text: str) -> str:
    """Remove any ``methods { ... }`` block from a per-contract spec."""
    return _METHODS_BLOCK_RE.sub("", spec_text).strip()


def _rule_names(spec_text: str) -> list[str]:
    return _RULE_DECL_RE.findall(spec_text)


def merge_per_contract_specs(
    per_contract: Iterable[tuple[str, str]],
    methods_block: str = "",
) -> MergeResult:
    """Merge per-contract CVL specs into one spec with exactly one methods block.

    ``per_contract`` is an iterable of ``(contract_name, spec_text)`` pairs. Any
    ``methods`` block embedded in a per-contract spec is stripped; the single
    provided ``methods_block`` is emitted once at the top (R16.7).

    Colliding rule names are renamed by appending the contract name, then an
    ascending integer on further collision, and each rename is recorded
    (R16.8).
    """
    renames: list[RuleRename] = []
    seen: set[str] = set()
    body_parts: list[str] = []

    for contract_name, spec_text in per_contract:
        body = _strip_methods_blocks(spec_text or "")
        # Resolve rule-name collisions deterministically, longest match first is
        # unnecessary since names are whole tokens; process in source order.
        for original in _rule_names(body):
            if original not in seen:
                seen.add(original)
                continue
            # Collision: append contract name, then ascending integer.
            candidate = f"{original}_{contract_name}"
            if candidate in seen:
                n = 2
                while f"{candidate}_{n}" in seen:
                    n += 1
                candidate = f"{candidate}_{n}"
            seen.add(candidate)
            body = _rename_rule(body, original, candidate)
            renames.append(
                RuleRename(original=original, renamed=candidate, contract=contract_name)
            )
        body_parts.append(body)

    parts: list[str] = []
    if methods_block:
        parts.append(methods_block.strip())
    parts.extend(p for p in body_parts if p)
    spec_text = "\n\n".join(parts).strip() + "\n"
    return MergeResult(spec_text=spec_text, renames=renames)


def _rename_rule(spec_text: str, original: str, renamed: str) -> str:
    """Rename the FIRST ``rule <original>`` declaration to ``rule <renamed>``.

    Only the declaration token is rewritten (the ``rule NAME`` at the collision
    site), leaving any earlier same-named declaration in other contracts intact.
    """
    pattern = re.compile(r"(\brule\s+)" + re.escape(original) + r"\b")
    return pattern.sub(lambda m: m.group(1) + renamed, spec_text, count=1)
