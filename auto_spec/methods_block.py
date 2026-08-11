"""Deterministic methods-block builder — removes LLM from methods-block generation entirely.

Parses Solidity source for public state vars, mappings, and function signatures,
then emits a correct methods block that never contains Solidity keywords.
"""

from __future__ import annotations

import re


def build_methods_block(contract_code: str) -> str:
    """Build a complete methods { ... } block from Solidity source.

    Emits getter declarations for public state vars/mappings and
    declarations for all external/public functions. Strips Solidity
    mutability keywords. Marks plain getters as envfree.
    """
    entries: list[str] = []
    seen: set[str] = set()

    # 1. Public state variable getters.
    #    Two passes so contracts with custom/struct/enum/array types still get a
    #    getter: first the common builtin types (exact match), then a general
    #    fallback for any other identifier type (`IERC20 public token`, `Foo[] public list`).
    #    `public constant` vars are included — CVL reads them through a getter too.
    for m in re.finditer(
        r'\b(?:(uint\d*|int\d*|address|bool|bytes\d*|string))\s+public\s+(?:constant\s+)?(\w+)',
        contract_code,
    ):
        name = m.group(2)
        if name in seen:
            continue
        seen.add(name)
        entries.append(f"    function {name}() external returns ({m.group(1)}) envfree;")

    for m in re.finditer(
        r'\b([A-Za-z_][A-Za-z0-9_]*(?:\s*\[\s*\d*\s*\])*)\s+public\s+(?:constant\s+)?(\w+)\s*[=;]',
        contract_code,
    ):
        name = m.group(2)
        if name in seen:
            continue
        seen.add(name)
        ret_type = m.group(1).strip()
        entries.append(f"    function {name}() external returns ({ret_type}) envfree;")

    # 2. Public mapping getters (single and double nesting; array-valued mappings)
    for m in re.finditer(
        r'mapping\s*\(([^()]*?)\s*=>\s*(?:mapping\s*\(([^()]*?)\s*=>\s*([^()]*?)\)|([^()]*?))\)\s+public\s+(\w+)',
        contract_code,
    ):
        name = m.group(5)
        if name in seen:
            continue
        seen.add(name)
        key_type = m.group(1).strip()
        if m.group(2):
            # nested mapping: mapping(A => mapping(B => C))
            inner_key = m.group(2).strip()
            val_type = m.group(3).strip()
            entries.append(f"    function {name}({key_type},{inner_key}) external returns ({val_type}) envfree;")
        else:
            val_type = m.group(4).strip()
            entries.append(f"    function {name}({key_type}) external returns ({val_type}) envfree;")

    # 3. External/public functions
    for m in re.finditer(
        r'function\s+(\w+)\s*\(([^)]*)\)\s+(external|public)\b[^{;]*?(?:returns\s*\(([^)]*)\))?',
        contract_code,
    ):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        params_raw = m.group(2).strip()
        returns_raw = m.group(4)

        params = _clean_params(params_raw) if params_raw else ""
        ret = f" returns ({_clean_type(returns_raw)})" if returns_raw else ""
        entries.append(f"    function {name}({params}) external{ret};")

    return "methods {\n" + "\n".join(entries) + "\n}"


def _clean_params(params: str) -> str:
    """Strip parameter names, keep only types (comma-separated)."""
    parts = []
    for p in params.split(","):
        p = p.strip()
        if not p:
            continue
        # Remove memory/storage/calldata qualifiers
        p = re.sub(r'\b(memory|storage|calldata)\b', '', p).strip()
        # Take only the type (first token)
        tokens = p.split()
        if tokens:
            parts.append(tokens[0])
    return ",".join(parts)


def _clean_type(type_str: str) -> str:
    """Clean a return type string (first token, e.g. `uint256[] memory` → `uint256[]`).

    Never defaults to a guessed type — an empty return means "no returns",
    and a wrong type here is worse than no type at all.
    """
    return type_str.strip().split()[0] if type_str and type_str.strip() else ""
