"""
Solidity Function Graph Analyzer

Uses slither-analyzer to parse Solidity files and extract semantic information
for RAG-based .spec file generation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


def _find_solc() -> str | None:
    """Find solc binary: check venv bin dir first, then PATH."""
    venv_solc = Path(sys.executable).parent / "solc"
    if venv_solc.is_file():
        return str(venv_solc)
    return shutil.which("solc")


def _auto_set_solc_version(sol_path: Path) -> None:
    """Parse pragma solidity from file and set SOLC_VERSION env var if not already set."""
    if os.environ.get("SOLC_VERSION"):
        return
    try:
        text = sol_path.read_text(errors="ignore")
        m = re.search(r"pragma\s+solidity\s+[\^>=<~]*\s*([\d.]+)", text)
        if m:
            os.environ["SOLC_VERSION"] = m.group(1)
    except OSError:
        pass


def _resolve_dep_roots(analyzed_path: Path) -> list[Path]:
    """Return shared Solidity dependency roots for ``analyzed_path``.

    Delegates to :class:`spec_pipeline.resolve.Dependency_Resolver`, which reads
    ``--deps-root`` values (not available here), the ``SOLIDITY_DEPS_ROOT``
    environment variable, and the ``node_modules`` / ``lib`` directories found by
    walking upward from the analyzed path (Requirements 7.1, 7.2). This replaces
    the former hardcoded ``_SHARED_DEPS`` constant.

    The import is local to avoid any import cycle: ``resolve.py`` is pure and does
    not import this module, so importing it here is safe and keeps ``resolve``
    off the slither-backed module-load path when it is not needed.
    """
    from spec_pipeline.resolve import Dependency_Resolver

    return Dependency_Resolver().resolve(analyzed_path).roots


def _build_solc_remaps(sol_path: Path, dep_roots: list[Path] | None = None) -> str:
    """Scan .sol file (or directory) for @-prefixed imports and return solc remapping args.

    ``dep_roots`` is the list of resolved dependency roots to search (from
    :class:`~spec_pipeline.resolve.Dependency_Resolver`). When omitted, the roots
    are resolved from ``sol_path``. Returns a string like
    '@openzeppelin/=/path/to/node_modules/@openzeppelin/' suitable for solc_args,
    or empty string if nothing to remap.
    """
    if dep_roots is None:
        dep_roots = _resolve_dep_roots(sol_path)
    dep_roots = [r for r in dep_roots if r.is_dir()]
    if not dep_roots:
        return ""

    # Collect all .sol files to scan
    if sol_path.is_file():
        files = [sol_path]
        # Also scan siblings / nearby .sol files (imports often pull from same project)
        files.extend(f for f in sol_path.parent.rglob("*.sol") if f != sol_path)
    else:
        files = list(sol_path.rglob("*.sol"))

    # Find unique @scope/package prefixes
    prefixes: set[str] = set()
    import_re = re.compile(r'import\s+.*?"(@[^/]+/[^/]+)/')
    for f in files:
        try:
            for m in import_re.finditer(f.read_text(errors="ignore")):
                prefixes.add(m.group(1))
        except OSError:
            continue

    # Build remappings for prefixes that exist under any resolved dependency root.
    # Handle common aliases: @openzeppelin-upgradeable/contracts -> @openzeppelin/contracts-upgradeable
    remaps = []
    for prefix in sorted(prefixes):
        for root in dep_roots:
            dep_path = root / prefix
            if dep_path.is_dir():
                remaps.append(f"{prefix}/={dep_path}/")
                break
            if prefix == "@openzeppelin-upgradeable/contracts":
                # Alias to @openzeppelin/contracts-upgradeable
                alias_path = root / "@openzeppelin" / "contracts-upgradeable"
                if alias_path.is_dir():
                    remaps.append(f"@openzeppelin-upgradeable/contracts/={alias_path}/")
                    break

    # Also handle non-@ imports like openzeppelin-solidity/
    non_at_re = re.compile(r'import\s+.*?"([^@./][^/]+/[^/]+)/')
    for f in files:
        try:
            text = f.read_text(errors="ignore")
            for m in non_at_re.finditer(text):
                prefix = m.group(1)
                for root in dep_roots:
                    dep_path = root / prefix
                    if dep_path.is_dir():
                        remaps.append(f"{prefix}/={dep_path}/")
                        break
                    if prefix == "openzeppelin-solidity/contracts":
                        # Map to @openzeppelin/contracts
                        alias_path = root / "@openzeppelin" / "contracts"
                        if alias_path.is_dir():
                            remaps.append(f"openzeppelin-solidity/contracts/={alias_path}/")
                            break
        except OSError:
            continue

    return " ".join(remaps)

# slither imports
from slither import Slither
from slither.core.declarations import (
    Contract,
    Function,
    FunctionContract,
    Modifier,
    Event,
    Enum,
    Structure as Struct,
    SolidityVariable,
)
from slither.core.variables import StateVariable
from slither.core.variables import Variable
from slither.core.solidity_types import UserDefinedType
from slither.core.cfg.node import Node, NodeType
from slither.slithir.operations import (
    HighLevelCall,
    LowLevelCall,
    SolidityCall,
    InternalCall,
    EventCall,
    InternalDynamicCall,
    LibraryCall,
)
from slither.utils.output import Output

import networkx as nx


@dataclass
class FunctionNode:
    """Represents a function in the call graph with rich metadata."""

    # Identity
    name: str
    full_name: str  # ContractName.functionName
    contract_name: str

    # Signature info
    visibility: str  # public, external, internal, private
    mutability: str  # view, pure, payable, nonpayable
    is_constructor: bool = False
    is_fallback: bool = False
    is_receive: bool = False
    modifiers: list[str] = field(default_factory=list)

    # Parameters and returns
    parameters: list[dict] = field(default_factory=list)
    return_types: list[str] = field(default_factory=list)

    # State interactions
    state_vars_read: list[str] = field(default_factory=list)
    state_vars_written: list[str] = field(default_factory=list)
    events_emitted: list[str] = field(default_factory=list)

    # Call graph
    internal_calls: list[str] = field(default_factory=list)  # function names called internally
    external_calls: list[dict] = field(default_factory=list)  # {target, function_name, type}

    # Source location
    source_file: str = ""
    line_start: int = 0
    line_end: int = 0

    # Full source code
    source_code: str = ""

    # ERC detection
    erc_interface: str = ""  # e.g., "ERC20", "ERC721", "ERC1155", "Ownable"

    # Security patterns
    has_reentrancy_guard: bool = False
    has_access_control: bool = False
    uses_merkle_proof: bool = False


@dataclass
class ContractInfo:
    """Represents a contract with its functions and state."""

    name: str
    kind: str  # contract, interface, library, abstract
    inheritance: list[str] = field(default_factory=list)
    implements_interfaces: list[str] = field(default_factory=list)

    # State
    state_variables: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    modifiers: list[dict] = field(default_factory=list)
    enums: list[dict] = field(default_factory=list)
    structs: list[dict] = field(default_factory=list)

    # Functions
    functions: list[FunctionNode] = field(default_factory=list)

    # Source
    source_file: str = ""
    source_code: str = ""


@dataclass
class SolidityGraph:
    """Complete graph representation of a Solidity project."""

    contracts: dict[str, ContractInfo] = field(default_factory=dict)
    call_graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    inheritance_graph: nx.DiGraph = field(default_factory=nx.DiGraph)

    def to_json(self) -> dict:
        """Export to JSON-serializable dict."""
        return {
            "contracts": {name: asdict(info) for name, info in self.contracts.items()},
            "call_graph_edges": list(self.call_graph.edges(data=True)),
            "inheritance_graph_edges": list(self.inheritance_graph.edges(data=True)),
        }

    def to_graphml(self) -> str:
        """Export to GraphML format for Gephi/Cytoscape."""
        # Create a combined graph for visualization
        G = nx.DiGraph()

        # Add contract nodes
        for cname, cinfo in self.contracts.items():
            G.add_node(f"contract:{cname}",
                      type="contract",
                      label=cname,
                      kind=cinfo.kind,
                      source_file=cinfo.source_file)

            # Add function nodes
            for func in cinfo.functions:
                fn_id = f"func:{cname}.{func.name}"
                G.add_node(fn_id,
                          type="function",
                          label=f"{cname}.{func.name}",
                          visibility=func.visibility,
                          mutability=func.mutability,
                          contract=cname)

                # Edge from contract to function
                G.add_edge(f"contract:{cname}", fn_id, type="contains")

                # Internal call edges
                for callee in func.internal_calls:
                    # Try to find callee in same contract
                    callee_id = f"func:{cname}.{callee}"
                    if callee_id in G.nodes or any(n.endswith(f".{callee}") for n in G.nodes):
                        G.add_edge(fn_id, callee_id, type="internal_call")

                # External call edges
                for ext_call in func.external_calls:
                    target = ext_call.get("target", "unknown")
                    fn_name = ext_call.get("function_name", "unknown")
                    ext_id = f"ext:{target}.{fn_name}"
                    G.add_edge(fn_id, ext_id, type="external_call", call_type=ext_call.get("call_type", ""))

                # State variable edges
                for svar in func.state_vars_read:
                    svar_id = f"state:{cname}.{svar}"
                    if svar_id not in G.nodes:
                        G.add_node(svar_id, type="state_var", label=svar, contract=cname)
                    G.add_edge(fn_id, svar_id, type="reads")

                for svar in func.state_vars_written:
                    svar_id = f"state:{cname}.{svar}"
                    if svar_id not in G.nodes:
                        G.add_node(svar_id, type="state_var", label=svar, contract=cname)
                    G.add_edge(fn_id, svar_id, type="writes")

                # Event edges
                for event in func.events_emitted:
                    evt_id = f"event:{cname}.{event}"
                    if evt_id not in G.nodes:
                        G.add_node(evt_id, type="event", label=event, contract=cname)
                    G.add_edge(fn_id, evt_id, type="emits")

        # Inheritance edges
        for cname, cinfo in self.contracts.items():
            for parent in cinfo.inheritance:
                if parent in self.contracts:
                    G.add_edge(f"contract:{cname}", f"contract:{parent}", type="inherits")

        # Convert to GraphML string - ensure all attributes are strings
        import io
        buffer = io.StringIO()

        # Create a new graph with all string attributes to avoid GraphML serialization issues
        G2 = nx.DiGraph()
        for node, attrs in G.nodes(data=True):
            str_attrs = {str(k): str(v) for k, v in attrs.items()}
            G2.add_node(str(node), **str_attrs)

        for u, v, attrs in G.edges(data=True):
            str_attrs = {str(k): str(v) for k, v in attrs.items()}
            G2.add_edge(str(u), str(v), **str_attrs)

        nx.write_graphml(G2, buffer)
        return buffer.getvalue()


class SolidityAnalyzer:
    """Analyzes Solidity files using slither to build function graphs."""

    # Known ERC interface patterns
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

    def __init__(self, project_path: str | Path, remappings: dict[str, Path] | None = None):
        self.project_path = Path(project_path).resolve()
        self.remappings = remappings or {}
        self.slither: Optional[Slither] = None
        self.graph = SolidityGraph()

    def analyze(self) -> SolidityGraph:
        """Run slither analysis and build the graph."""
        solc = _find_solc()
        solc_kw: dict[str, Any] = {}
        if solc:
            solc_kw["solc"] = solc
        # Auto-detect import remappings for scraped repos missing node_modules,
        # using the shared dependency roots from the Dependency_Resolver.
        dep_roots = _resolve_dep_roots(self.project_path)
        remaps = _build_solc_remaps(self.project_path, dep_roots)
        if remaps:
            solc_kw["solc_remaps"] = remaps
            allow_paths = ",".join(str(r) for r in dep_roots)
            solc_kw["solc_args"] = f"--allow-paths {allow_paths}"
        # Try to initialize slither
        try:
            # Check if project_path is a single .sol file
            if self.project_path.is_file() and self.project_path.suffix == ".sol":
                _auto_set_solc_version(self.project_path)
                self.slither = Slither(str(self.project_path), **solc_kw)
            else:
                # First, check if we have a foundry project
                foundry_toml = self.project_path / "foundry.toml"
                if foundry_toml.exists():
                    self.slither = Slither(str(self.project_path), **solc_kw)
                else:
                    # Try to find sol files directly
                    sol_files = list(self.project_path.rglob("*.sol"))
                    if not sol_files:
                        raise ValueError(f"No .sol files found in {self.project_path}")
                    # Use the first .sol file as entry point
                    _auto_set_solc_version(sol_files[0])
                    self.slither = Slither(str(sol_files[0]), **solc_kw)
        except Exception as e:
            # Fallback: try crytic-compile approach
            print(f"Slither init failed: {e}")
            raise

        # Process each contract
        for contract in self.slither.contracts:
            self._analyze_contract(contract)

        # Build call graph edges
        self._build_call_graph()
        self._build_inheritance_graph()

        return self.graph

    def _analyze_contract(self, contract: Contract) -> None:
        """Extract all info from a single contract."""
        sm = getattr(contract, "source_mapping", None)
        # Extract actual absolute path from slither's Filename object
        src_file = ""
        if sm and sm.filename:
            try:
                # Filename object has .absolute property for absolute path
                if hasattr(sm.filename, 'absolute'):
                    src_file = str(sm.filename.absolute)
                else:
                    src_file = str(sm.filename)
            except Exception:
                src_file = str(sm.filename) if sm.filename else ""

        cinfo = ContractInfo(
            name=contract.name,
            kind=contract.contract_kind.lower() if contract.contract_kind else "contract",
            source_file=src_file,
        )

        # Get full source code
        try:
            cinfo.source_code = sm.content if sm else ""
        except Exception:
            cinfo.source_code = ""

        # Inheritance
        cinfo.inheritance = [c.name for c in contract.inheritance]

        # Implemented interfaces
        for interface in getattr(contract, "implemented_interfaces", []):
            cinfo.implements_interfaces.append(interface.name)

        # State variables
        for var in contract.state_variables_declared:
            cinfo.state_variables.append({
                "name": var.name,
                "type": str(var.type),
                "visibility": var.visibility,
                "is_constant": var.is_constant,
                "is_immutable": var.is_immutable,
                "initial_value": str(var.expression) if var.expression else None,
            })

        # Events
        for evt in contract.events:
            cinfo.events.append({
                "name": evt.name,
                "parameters": [
                    {"name": p.name, "type": str(p.type), "indexed": getattr(p, "indexed", False)}
                    for p in getattr(evt, "elems", getattr(evt, "parameters", []))
                ],
            })

        # Modifiers
        for mod in contract.modifiers:
            cinfo.modifiers.append({
                "name": mod.name,
                "parameters": [{"name": p.name, "type": str(p.type)} for p in mod.parameters],
            })

        # Enums
        for enum in contract.enums:
            cinfo.enums.append({
                "name": enum.name,
                "values": [v for v in getattr(enum, "values", [])],
            })

        # Structs
        for struct in getattr(contract, "structures", getattr(contract, "structs", [])):
            elems = getattr(struct, "elems", getattr(struct, "fields", {}))
            if isinstance(elems, dict):
                elems = elems.values()
            cinfo.structs.append({
                "name": struct.name,
                "fields": [{"name": f.name, "type": str(f.type)} for f in elems],
            })

        # Functions
        for func in contract.functions:
            if func.contract_declarer != contract:
                continue  # Skip inherited functions (we'll get them from parent)

            fnode = self._analyze_function(func, contract)
            cinfo.functions.append(fnode)

            # Add to call graph nodes
            self.graph.call_graph.add_node(f"{contract.name}.{func.name}",
                                          contract=contract.name,
                                          function=func.name)

        self.graph.contracts[contract.name] = cinfo

    def _analyze_function(self, func: Function, contract: Contract) -> FunctionNode:
        """Extract detailed info from a function."""
        # Parameters
        params = []
        for p in func.parameters:
            params.append({
                "name": p.name,
                "type": str(p.type),
            })

        # Return types
        returns = []
        for r in (func.return_type or []):
            returns.append(str(r))

        # Modifiers
        modifiers = [m.name for m in func.modifiers]

        # State variables read/written
        state_read = []
        state_written = []
        for var in func.state_variables_read:
            if isinstance(var, StateVariable):
                state_read.append(var.name)
        for var in func.state_variables_written:
            if isinstance(var, StateVariable):
                state_written.append(var.name)

        # Events emitted
        events = []
        for ir in func.all_slithir_operations():
            if isinstance(ir, EventCall):
                evt = getattr(ir, "event", None)
                if evt:
                    events.append(evt.name)

        # Internal calls
        internal_calls = []
        external_calls = []

        for ir in func.all_slithir_operations():
            if isinstance(ir, InternalCall):
                if ir.function:
                    internal_calls.append(ir.function.name)
            elif isinstance(ir, (HighLevelCall, LowLevelCall, LibraryCall)):
                call_info = {
                    "target": self._get_call_target(ir),
                    "function_name": self._get_call_function(ir),
                    "call_type": type(ir).__name__,
                }
                external_calls.append(call_info)
            elif isinstance(ir, SolidityCall):
                # Built-in calls like address.call, abi.encode, etc.
                call_info = {
                    "target": "solidity_builtin",
                    "function_name": ir.function.name if ir.function else str(ir),
                    "call_type": "SolidityCall",
                }
                external_calls.append(call_info)

        # Security pattern detection
        has_reentrancy = any("nonReentrant" in m for m in modifiers)
        has_access = any(m in ["onlyOwner", "onlyRole", "authenticated"] for m in modifiers)
        has_merkle = any("merkle" in str(ir).lower() for ir in func.all_slithir_operations())

        # ERC interface detection
        erc = self._detect_erc_interface(func, contract)

        # Source code
        sm = getattr(func, "source_mapping", None)
        source = ""
        try:
            source = sm.content if sm else ""
        except Exception:
            pass

        # Source location
        src_file = ""
        line_start = 0
        line_end = 0
        if sm:
            try:
                src_file = str(sm.filename.absolute()) if hasattr(sm.filename, "absolute") else str(sm.filename)
            except Exception:
                pass
            lines = getattr(sm, "lines", [])
            if lines:
                line_start = lines[0]
                line_end = lines[-1]

        return FunctionNode(
            name=func.name,
            full_name=f"{contract.name}.{func.name}",
            contract_name=contract.name,
            visibility=func.visibility,
            mutability="view" if func.view else ("pure" if func.pure else ("payable" if func.payable else "nonpayable")),
            is_constructor=func.is_constructor,
            is_fallback=func.is_fallback,
            is_receive=func.is_receive,
            modifiers=modifiers,
            parameters=params,
            return_types=returns,
            state_vars_read=state_read,
            state_vars_written=state_written,
            events_emitted=events,
            internal_calls=internal_calls,
            external_calls=external_calls,
            source_file=src_file,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            erc_interface=erc,
            has_reentrancy_guard=has_reentrancy,
            has_access_control=has_access,
            uses_merkle_proof=has_merkle,
        )

    def _get_call_target(self, ir) -> str:
        """Extract target contract/address from a call."""
        if isinstance(ir, HighLevelCall):
            if ir.destination:
                return str(ir.destination)
        elif isinstance(ir, LowLevelCall):
            if ir.destination:
                return str(ir.destination)
        elif isinstance(ir, LibraryCall):
            if ir.library:
                return ir.library.name
        return "unknown"

    def _get_call_function(self, ir) -> str:
        """Extract function name from a call."""
        if isinstance(ir, HighLevelCall):
            if ir.function:
                return ir.function.name
        elif isinstance(ir, LowLevelCall):
            if ir.function_name:
                return ir.function_name
        return "unknown"

    def _detect_erc_interface(self, func: Function, contract: Contract) -> str:
        """Detect if contract implements a known ERC interface."""
        func_names = {f.name for f in contract.functions}

        for erc_name, required_funcs in self.ERC_PATTERNS.items():
            if all(any(req in fn for fn in func_names) for req in required_funcs):
                return erc_name
        return ""

    def _build_call_graph(self) -> None:
        """Build the networkx call graph."""
        for contract_name, cinfo in self.graph.contracts.items():
            for func in cinfo.functions:
                caller_id = f"{contract_name}.{func.name}"

                # Internal calls
                for callee_name in func.internal_calls:
                    callee_id = f"{contract_name}.{callee_name}"
                    if callee_id in self.graph.call_graph.nodes or any(n.endswith(f".{callee_name}") for n in self.graph.call_graph.nodes):
                        self.graph.call_graph.add_edge(caller_id, callee_id, type="internal")

                # External calls
                for ext_call in func.external_calls:
                    target = ext_call.get("target", "unknown")
                    fn_name = ext_call.get("function_name", "unknown")
                    ext_id = f"{target}.{fn_name}"
                    self.graph.call_graph.add_edge(caller_id, ext_id,
                                                  type="external",
                                                  call_type=ext_call.get("call_type", ""))

    def _build_inheritance_graph(self) -> None:
        """Build inheritance graph."""
        for cname, cinfo in self.graph.contracts.items():
            self.graph.inheritance_graph.add_node(cname)
            for parent in cinfo.inheritance:
                if parent in self.graph.contracts:
                    self.graph.inheritance_graph.add_edge(cname, parent)

    def export_json(self, output_path: str | Path) -> None:
        """Export graph to JSON file."""
        with open(output_path, "w") as f:
            json.dump(self.graph.to_json(), f, indent=2, default=str)

    def export_graphml(self, output_path: str | Path) -> None:
        """Export graph to GraphML file."""
        # Use networkx's file writing directly to avoid StringIO issues
        G = nx.DiGraph()

        # Add contract nodes
        for cname, cinfo in self.graph.contracts.items():
            G.add_node(f"contract:{cname}",
                       type="contract",
                       label=cname,
                       kind=cinfo.kind,
                       source_file=cinfo.source_file)

            # Add function nodes
            for func in cinfo.functions:
                fn_id = f"func:{cname}.{func.name}"
                G.add_node(fn_id,
                          type="function",
                          label=f"{cname}.{func.name}",
                          visibility=func.visibility,
                          mutability=func.mutability,
                          contract=cname)

                # Edge from contract to function
                G.add_edge(f"contract:{cname}", fn_id, type="contains")

                # Internal call edges
                for callee in func.internal_calls:
                    callee_id = f"func:{cname}.{callee}"
                    if callee_id in G.nodes or any(n.endswith(f".{callee}") for n in G.nodes):
                        G.add_edge(fn_id, callee_id, type="internal_call")

                # External call edges
                for ext_call in func.external_calls:
                    target = ext_call.get("target", "unknown")
                    fn_name = ext_call.get("function_name", "unknown")
                    ext_id = f"ext:{target}.{fn_name}"
                    G.add_edge(fn_id, ext_id, type="external_call", call_type=ext_call.get("call_type", ""))

                # State variable edges
                for svar in func.state_vars_read:
                    svar_id = f"state:{cname}.{svar}"
                    if svar_id not in G.nodes:
                        G.add_node(svar_id, type="state_var", label=svar, contract=cname)
                    G.add_edge(fn_id, svar_id, type="reads")

                for svar in func.state_vars_written:
                    svar_id = f"state:{cname}.{svar}"
                    if svar_id not in G.nodes:
                        G.add_node(svar_id, type="state_var", label=svar, contract=cname)
                    G.add_edge(fn_id, svar_id, type="writes")

                # Event edges
                for event in func.events_emitted:
                    evt_id = f"event:{cname}.{event}"
                    if evt_id not in G.nodes:
                        G.add_node(evt_id, type="event", label=event, contract=cname)
                    G.add_edge(fn_id, evt_id, type="emits")

        # Inheritance edges
        for cname, cinfo in self.graph.contracts.items():
            for parent in cinfo.inheritance:
                if parent in self.graph.contracts:
                    G.add_edge(f"contract:{cname}", f"contract:{parent}", type="inherits")

        # Write directly to file
        nx.write_graphml(G, str(output_path))


def analyze_path(
    path: str | Path,
    output_dir: str | Path | None = None,
    formats: list[str] | None = None,
) -> SolidityGraph:
    """
    Analyze a Solidity project and export results.

    Args:
        path: Path to .sol file or directory containing .sol files
        output_dir: Directory to write outputs (default: same as path)
        formats: List of output formats: ["json", "graphml", "rag"]

    Returns:
        SolidityGraph object with all analysis results
    """
    path = Path(path).resolve()
    formats = formats or ["json"]

    if output_dir is None:
        output_dir = path if path.is_dir() else path.parent
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Analyzing {path}...")
    analyzer = SolidityAnalyzer(path)
    graph = analyzer.analyze()

    print(f"Found {len(graph.contracts)} contracts")
    total_funcs = sum(len(c.functions) for c in graph.contracts.values())
    print(f"Found {total_funcs} functions")
    print(f"Call graph edges: {graph.call_graph.number_of_edges()}")

    # Export requested formats
    base_name = path.stem if path.is_file() else path.name

    if "json" in formats:
        out_file = output_dir / f"{base_name}_graph.json"
        analyzer.export_json(out_file)
        print(f"Exported JSON to {out_file}")

    if "graphml" in formats:
        out_file = output_dir / f"{base_name}_graph.graphml"
        analyzer.export_graphml(out_file)
        print(f"Exported GraphML to {out_file}")

    if "rag" in formats:
        from .rag_exporter import export_rag_chunks
        out_file = output_dir / f"{base_name}_rag_chunks.json"
        export_rag_chunks(graph, out_file)
        print(f"Exported RAG chunks to {out_file}")

    return graph