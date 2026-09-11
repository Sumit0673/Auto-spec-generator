"""
RAG Exporter for Solidity Function Graphs

Converts SolidityGraph into chunks optimized for embedding and retrieval
in a vector database (Chroma) for spec generation tasks.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from solidity_graph.analyzer import SolidityGraph, ContractInfo, FunctionNode


def export_rag_chunks(graph: SolidityGraph, output_path: str | Path) -> list[dict]:
    """
    Export SolidityGraph as RAG-optimized chunks.

    Each chunk contains:
    - Text content for embedding
    - Rich metadata for filtering/retrieval
    - Cross-references to related chunks

    Chunk types:
    - contract_overview: High-level contract summary
    - function: Individual function with full context
    - state_variable: State variable with read/write patterns
    - modifier: Modifier definition and usage
    - event: Event definition and emission points
    - inheritance: Inheritance relationships
    - erc_interface: Detected ERC standard compliance
    """
    chunks = []

    for cname, cinfo in graph.contracts.items():
        # 1. Contract overview chunk
        chunks.append(_make_contract_overview_chunk(cinfo))

        # 2. Function chunks (one per function)
        for func in cinfo.functions:
            chunks.append(_make_function_chunk(func, cinfo, graph))

        # 3. State variable chunks
        for svar in cinfo.state_variables:
            chunks.append(_make_state_var_chunk(svar, cinfo, graph))

        # 4. Modifier chunks
        for mod in cinfo.modifiers:
            chunks.append(_make_modifier_chunk(mod, cinfo, graph))

        # 5. Event chunks
        for evt in cinfo.events:
            chunks.append(_make_event_chunk(evt, cinfo, graph))

        # 6. Inheritance chunk
        if cinfo.inheritance or cinfo.implements_interfaces:
            chunks.append(_make_inheritance_chunk(cinfo, graph))

        # 7. ERC interface chunk
        erc = _detect_contract_erc(cinfo)
        if erc:
            chunks.append(_make_erc_chunk(cinfo, erc))

    # Write to file
    with open(output_path, "w") as f:
        json.dump(chunks, f, indent=2, default=str)

    print(f"Generated {len(chunks)} RAG chunks")
    return chunks


def _make_contract_overview_chunk(cinfo: ContractInfo) -> dict:
    """Create a high-level contract summary chunk."""
    func_summary = []
    for f in cinfo.functions:
        func_summary.append(f"{f.visibility} {f.mutability} {f.name}({', '.join(p['type'] for p in f.parameters)})")

    state_summary = [f"{v['visibility']} {v['type']} {v['name']}" for v in cinfo.state_variables]
    event_summary = [f"event {e['name']}({', '.join(f'{p['type']} {p['name']}' for p in e['parameters'])})" for e in cinfo.events]

    content = f"""Contract: {cinfo.name} ({cinfo.kind})
Inheritance: {', '.join(cinfo.inheritance) if cinfo.inheritance else 'none'}
Implements: {', '.join(cinfo.implements_interfaces) if cinfo.implements_interfaces else 'none'}

Functions:
{chr(10).join(func_summary) if func_summary else '  (none)'}

State Variables:
{chr(10).join(state_summary) if state_summary else '  (none)'}

Events:
{chr(10).join(event_summary) if event_summary else '  (none)'}
"""

    return {
        "id": f"contract:{cinfo.name}:overview",
        "type": "contract_overview",
        "content": content.strip(),
        "metadata": {
            "contract_name": cinfo.name,
            "contract_kind": cinfo.kind,
            "source_file": cinfo.source_file,
            "function_count": len(cinfo.functions),
            "state_var_count": len(cinfo.state_variables),
            "event_count": len(cinfo.events),
            "inheritance": cinfo.inheritance,
            "implements": cinfo.implements_interfaces,
        },
        "references": [f"function:{cinfo.name}:{f.name}" for f in cinfo.functions],
    }


def _make_function_chunk(func: FunctionNode, cinfo: ContractInfo, graph: SolidityGraph) -> dict:
    """Create a detailed function chunk with full context for spec generation."""
    # Build parameter string
    params_str = ", ".join(f"{p['type']} {p['name']}" for p in func.parameters)
    returns_str = ", ".join(func.return_types) if func.return_types else "void"

    # Build modifier info
    mod_str = ", ".join(func.modifiers) if func.modifiers else "none"

    # Called functions detail
    called_detail = []
    for call in func.internal_calls:
        called_detail.append(f"  internal: {call}")
    for call in func.external_calls:
        called_detail.append(f"  external ({call.get('call_type', '')}): {call.get('target', '')}.{call.get('function_name', '')}")

    content = f"""Function: {func.full_name}
Contract: {cinfo.name} ({cinfo.kind})
Visibility: {func.visibility}
Mutability: {func.mutability}
Modifiers: {mod_str}
Signature: {func.name}({params_str}) -> {returns_str}
Constructor: {func.is_constructor}
Fallback: {func.is_fallback}
Receive: {func.is_receive}

State Variables Read:
{chr(10).join(f"  {v}" for v in func.state_vars_read) if func.state_vars_read else '  (none)'}

State Variables Written:
{chr(10).join(f"  {v}" for v in func.state_vars_written) if func.state_vars_written else '  (none)'}

Events Emitted:
{chr(10).join(f"  {e}" for e in func.events_emitted) if func.events_emitted else '  (none)'}

Internal Calls:
{chr(10).join(called_detail) if called_detail else '  (none)'}

Security Patterns:
  Reentrancy Guard: {func.has_reentrancy_guard}
  Access Control: {func.has_access_control}
  Merkle Proof: {func.uses_merkle_proof}

ERC Interface: {func.erc_interface if func.erc_interface else 'none'}

Source Code:
```solidity
{func.source_code if func.source_code else '(source not available)'}
```
"""

    # Find caller functions (reverse edges in call graph)
    callers = []
    caller_node = f"{cinfo.name}.{func.name}"
    if caller_node in graph.call_graph:
        for pred in graph.call_graph.predecessors(caller_node):
            callers.append(pred)

    return {
        "id": f"function:{cinfo.name}:{func.name}",
        "type": "function",
        "content": content.strip(),
        "metadata": {
            "contract_name": cinfo.name,
            "contract_kind": cinfo.kind,
            "function_name": func.name,
            "full_name": func.full_name,
            "visibility": func.visibility,
            "mutability": func.mutability,
            "modifiers": func.modifiers,
            "is_constructor": func.is_constructor,
            "is_fallback": func.is_fallback,
            "is_receive": func.is_receive,
            "parameters": func.parameters,
            "return_types": func.return_types,
            "state_vars_read": func.state_vars_read,
            "state_vars_written": func.state_vars_written,
            "events_emitted": func.events_emitted,
            "internal_calls": func.internal_calls,
            "external_calls": func.external_calls,
            "has_reentrancy_guard": func.has_reentrancy_guard,
            "has_access_control": func.has_access_control,
            "uses_merkle_proof": func.uses_merkle_proof,
            "erc_interface": func.erc_interface,
            "source_file": func.source_file,
            "line_start": func.line_start,
            "line_end": func.line_end,
            "callers": callers,
        },
        "references": [
            f"contract:{cinfo.name}:overview",
            *[f"state_var:{cinfo.name}:{v}" for v in func.state_vars_read + func.state_vars_written],
            *[f"event:{cinfo.name}:{e}" for e in func.events_emitted],
            *[f"function:{cinfo.name}:{c}" for c in func.internal_calls],
        ],
    }


def _make_state_var_chunk(svar: dict, cinfo: ContractInfo, graph: SolidityGraph) -> dict:
    """Create a state variable chunk with read/write context."""
    # Find functions that read/write this variable
    readers = []
    writers = []
    for func in cinfo.functions:
        if svar["name"] in func.state_vars_read:
            readers.append(func.name)
        if svar["name"] in func.state_vars_written:
            writers.append(func.name)

    content = f"""State Variable: {cinfo.name}.{svar['name']}
Type: {svar['type']}
Visibility: {svar['visibility']}
Constant: {svar['is_constant']}
Immutable: {svar['is_immutable']}
Initial Value: {svar['initial_value'] if svar['initial_value'] else 'none'}

Read by functions:
{chr(10).join(f"  {r}" for r in readers) if readers else '  (none)'}

Written by functions:
{chr(10).join(f"  {w}" for w in writers) if writers else '  (none)'}
"""

    return {
        "id": f"state_var:{cinfo.name}:{svar['name']}",
        "type": "state_variable",
        "content": content.strip(),
        "metadata": {
            "contract_name": cinfo.name,
            "variable_name": svar["name"],
            "type": svar["type"],
            "visibility": svar["visibility"],
            "is_constant": svar["is_constant"],
            "is_immutable": svar["is_immutable"],
            "initial_value": svar["initial_value"],
            "read_by": readers,
            "written_by": writers,
        },
        "references": [
            f"contract:{cinfo.name}:overview",
            *[f"function:{cinfo.name}:{r}" for r in readers],
            *[f"function:{cinfo.name}:{w}" for w in writers],
        ],
    }


def _make_modifier_chunk(mod: dict, cinfo: ContractInfo, graph: SolidityGraph) -> dict:
    """Create a modifier chunk with usage context."""
    # Find functions using this modifier
    users = []
    for func in cinfo.functions:
        if mod["name"] in func.modifiers:
            users.append(func.name)

    params_str = ", ".join(f"{p['type']} {p['name']}" for p in mod["parameters"])

    content = f"""Modifier: {cinfo.name}.{mod['name']}
Parameters: ({params_str})

Used by functions:
{chr(10).join(f"  {u}" for u in users) if users else '  (none)'}
"""

    return {
        "id": f"modifier:{cinfo.name}:{mod['name']}",
        "type": "modifier",
        "content": content.strip(),
        "metadata": {
            "contract_name": cinfo.name,
            "modifier_name": mod["name"],
            "parameters": mod["parameters"],
            "used_by": users,
        },
        "references": [
            f"contract:{cinfo.name}:overview",
            *[f"function:{cinfo.name}:{u}" for u in users],
        ],
    }


def _make_event_chunk(evt: dict, cinfo: ContractInfo, graph: SolidityGraph) -> dict:
    """Create an event chunk with emission context."""
    # Find functions emitting this event
    emitters = []
    for func in cinfo.functions:
        if evt["name"] in func.events_emitted:
            emitters.append(func.name)

    params_str = ", ".join(f"{p['type']} {p['name']}{' indexed' if p['indexed'] else ''}" for p in evt["parameters"])

    content = f"""Event: {cinfo.name}.{evt['name']}
Parameters: ({params_str})
Anonymous: {evt.get('anonymous', False)}

Emitted by functions:
{chr(10).join(f"  {e}" for e in emitters) if emitters else '  (none)'}
"""

    return {
        "id": f"event:{cinfo.name}:{evt['name']}",
        "type": "event",
        "content": content.strip(),
        "metadata": {
            "contract_name": cinfo.name,
            "event_name": evt["name"],
            "parameters": evt["parameters"],
            "anonymous": evt.get("anonymous", False),
            "emitted_by": emitters,
        },
        "references": [
            f"contract:{cinfo.name}:overview",
            *[f"function:{cinfo.name}:{e}" for e in emitters],
        ],
    }


def _make_inheritance_chunk(cinfo: ContractInfo, graph: SolidityGraph) -> dict:
    """Create an inheritance relationship chunk."""
    content = f"""Contract Inheritance: {cinfo.name}
Direct Parents: {', '.join(cinfo.inheritance) if cinfo.inheritance else 'none'}
Implements Interfaces: {', '.join(cinfo.implements_interfaces) if cinfo.implements_interfaces else 'none'}

This contract inherits from the above parents, gaining their functions,
state variables, and modifiers. Interface implementations must satisfy
all external function signatures.
"""

    return {
        "id": f"inheritance:{cinfo.name}",
        "type": "inheritance",
        "content": content.strip(),
        "metadata": {
            "contract_name": cinfo.name,
            "parents": cinfo.inheritance,
            "interfaces": cinfo.implements_interfaces,
        },
        "references": [
            f"contract:{cinfo.name}:overview",
            *[f"contract:{p}:overview" for p in cinfo.inheritance if p in graph.contracts],
        ],
    }


def _detect_contract_erc(cinfo: ContractInfo) -> str:
    """Detect ERC standard from contract functions."""
    func_names = {f.name for f in cinfo.functions}

    ERC_PATTERNS = {
        "ERC20": ["transfer", "approve", "allowance", "balanceOf", "totalSupply", "transferFrom"],
        "ERC721": ["ownerOf", "safeTransferFrom", "transferFrom", "approve", "getApproved", "setApprovalForAll", "isApprovedForAll"],
        "ERC1155": ["balanceOf", "balanceOfBatch", "setApprovalForAll", "isApprovedForAll", "safeTransferFrom", "safeBatchTransferFrom"],
        "Ownable": ["owner", "transferOwnership", "renounceOwnership"],
        "Pausable": ["pause", "unpause", "paused"],
        "ReentrancyGuard": ["nonReentrant", "_nonReentrantBefore", "_nonReentrantAfter"],
        "AccessControl": ["hasRole", "grantRole", "revokeRole", "DEFAULT_ADMIN_ROLE"],
        "EIP712": ["_domainSeparatorV4", "_hashTypedDataV4"],
        "Multicall": ["multicall"],
        "ERC165": ["supportsInterface"],
    }

    for erc_name, required_funcs in ERC_PATTERNS.items():
        matches = sum(1 for req in required_funcs if any(req in fn for fn in func_names))
        if matches >= len(required_funcs) * 0.7:  # 70% match threshold
            return erc_name
    return ""


def _make_erc_chunk(cinfo: ContractInfo, erc: str) -> dict:
    """Create an ERC compliance chunk."""
    content = f"""ERC Interface Compliance: {cinfo.name} -> {erc}

This contract implements the {erc} standard interface.
When generating specifications, consider {erc}-specific properties:
"""

    if erc == "ERC20":
        content += """- Total supply invariant: sum of balances == totalSupply
- Transfer decreases sender balance, increases receiver balance
- Approve sets allowance, transferFrom consumes it
- BalanceOf and allowance are view functions
"""
    elif erc == "ERC721":
        content += """- Each token ID has exactly one owner
- TransferFrom and safeTransferFrom transfer ownership
- Approve and setApprovalForAll manage approvals
- OwnerOf returns current owner
"""
    elif erc == "Ownable":
        content += """- Only owner can call owner-only functions
- TransferOwnership changes owner
- RenounceOwnership sets owner to address(0)
"""
    elif erc == "ReentrancyGuard":
        content += """- NonReentrant modifier prevents reentrant calls
- State changes before external calls in nonReentrant functions
"""
    elif erc == "AccessControl":
        content += """- Role-based access control with DEFAULT_ADMIN_ROLE
- GrantRole/revokeRole manage permissions
- HasRole checks authorization
"""

    return {
        "id": f"erc:{cinfo.name}:{erc}",
        "type": "erc_interface",
        "content": content.strip(),
        "metadata": {
            "contract_name": cinfo.name,
            "erc_standard": erc,
        },
        "references": [
            f"contract:{cinfo.name}:overview",
            *[f"function:{cinfo.name}:{f.name}" for f in cinfo.functions if f.erc_interface == erc],
        ],
    }


def load_rag_chunks(input_path: str | Path) -> list[dict]:
    """Load RAG chunks from JSON file."""
    with open(input_path) as f:
        return json.load(f)


def filter_chunks_for_spec_generation(
    chunks: list[dict],
    contract_name: str,
    function_names: list[str] | None = None,
    include_state: bool = True,
    include_events: bool = True,
    include_modifiers: bool = True,
    include_inheritance: bool = True,
    include_erc: bool = True,
) -> list[dict]:
    """Filter chunks relevant for generating a spec for specific functions."""
    filtered = []

    for chunk in chunks:
        meta = chunk.get("metadata", {})

        # Must belong to target contract
        if meta.get("contract_name") != contract_name:
            continue

        ctype = chunk.get("type")

        if ctype == "contract_overview":
            filtered.append(chunk)
        elif ctype == "function":
            if function_names is None or meta.get("function_name") in function_names:
                filtered.append(chunk)
        elif ctype == "state_variable" and include_state:
            filtered.append(chunk)
        elif ctype == "event" and include_events:
            filtered.append(chunk)
        elif ctype == "modifier" and include_modifiers:
            filtered.append(chunk)
        elif ctype == "inheritance" and include_inheritance:
            filtered.append(chunk)
        elif ctype == "erc_interface" and include_erc:
            filtered.append(chunk)

    return filtered