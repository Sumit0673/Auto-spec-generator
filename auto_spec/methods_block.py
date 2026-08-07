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

    # 1. Public state variable getters
    for m in re.finditer(
        r'(?:uint\d*|int\d*|address|bool|bytes\d*|string)\s+public\s+(\w+)',
        contract_code,
    ):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ret_type = _extract_return_type(m.group(0))
        entries.append(f"    function {name}() external returns ({ret_type}) envfree;")

    # 2. Public mapping getters
    for m in re.finditer(
        r'mapping\s*\((\w+)\s*=>\s*(?:mapping\s*\((\w+)\s*=>\s*(\w+)\)|(\w+))\)\s+public\s+(\w+)',
        contract_code,
    ):
        name = m.group(5)
        if name in seen:
            continue
        seen.add(name)
        key_type = m.group(1)
        if m.group(2):
            # nested mapping: mapping(A => mapping(B => C))
            inner_key = m.group(2)
            val_type = m.group(3)
            entries.append(f"    function {name}({key_type},{inner_key}) external returns ({val_type}) envfree;")
        else:
            val_type = m.group(4)
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


def _extract_return_type(declaration: str) -> str:
    """Extract the Solidity type from a state variable declaration."""
    match = re.match(r'(uint\d*|int\d*|address|bool|bytes\d*|string)', declaration.strip())
    return match.group(1) if match else "uint256"


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
    """Clean a return type string."""
    return type_str.strip().split()[0] if type_str else "uint256"
