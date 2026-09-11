"""
Stage 1: First-Party Extraction

Uses slither-analyzer to parse Solidity files and extract a COMPACT table of
first-party contracts only (drops OZ, libraries, interfaces from node_modules).

Output per contract:
- State variables + writers (which functions write them)
- External/public functions + their access-control modifier (one-liner)
- First-party caller graph (cross-contract only, drops internal/private, drops OZ)

Goal: Under a few hundred lines per contract, not thousands.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# Add parent directory for solidity_graph imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from solidity_graph.analyzer import (
    SolidityAnalyzer,
    SolidityGraph,
    ContractInfo,
    FunctionNode,
    _find_solc,
    _build_solc_remaps,
)


class CompileError(Exception):
    """Raised when slither/solc cannot compile the analyzed project (R19.6).

    The Extractor raises this instead of letting a raw slither exception escape,
    carrying the compiler diagnostics, the attempted solc version, and the
    applied remappings so the orchestrator can report the ``compile_failed``
    outcome (exit 7) without writing a Stage 1 artifact or proceeding to Stage 2.

    Attributes:
        diagnostics: The compiler / slither error text.
        solc_version: The attempted solc version (or ``"unknown"``).
        remappings: The import remappings applied for the compile attempt.
    """

    def __init__(
        self,
        diagnostics: str,
        solc_version: str = "unknown",
        remappings: Optional[list[str]] = None,
    ):
        self.diagnostics = diagnostics
        self.solc_version = solc_version
        self.remappings = list(remappings or [])
        super().__init__(
            f"compile_failed (solc={solc_version}): {diagnostics}"
        )


@dataclass
class StateVarInfo:
    """Compact state variable info with writers."""
    name: str
    type: str
    visibility: str
    is_constant: bool
    is_immutable: bool
    writers: list[str] = field(default_factory=list)  # function names that write this


@dataclass
class FunctionGate:
    """External/public function with its access gate."""
    name: str
    signature: str  # e.g., "setPoolConfig(uint256,PoolConfig)"
    visibility: str  # public, external
    mutability: str  # view, pure, payable, nonpayable
    modifier: str  # the access control modifier, or "none"
    is_constructor: bool = False
    is_fallback: bool = False
    is_receive: bool = False


@dataclass
class CallerEdge:
    """Cross-contract call edge (first-party only)."""
    caller_contract: str
    caller_function: str
    callee_contract: str
    callee_function: str
    call_type: str  # "internal" (same contract), "external" (first-party), "library" (dropped)


@dataclass
class DelegationEdge:
    """A delegatecall edge for a proxy contract (Requirement 19.4).

    Records one delegation per delegating function: which contract and function
    delegate, and the state variable that holds the delegation target address.
    ``target_state_var`` is ``"unresolved"`` when no single state variable holds
    the target (e.g. the target is computed or passed in), so the edge is still
    recorded rather than dropped.
    """
    proxy_contract: str
    delegating_function: str
    target_state_var: str  # state var holding the target address, or "unresolved"


@dataclass
class FirstPartyContract:
    """Compact representation of a first-party contract."""
    name: str
    kind: str  # contract, interface, library
    source_file: str
    state_vars: list[StateVarInfo] = field(default_factory=list)
    function_gates: list[FunctionGate] = field(default_factory=list)
    caller_edges: list[CallerEdge] = field(default_factory=list)
    # R19.4: proxy detection. ``is_proxy`` is True when this contract delegates
    # calls to an address held in state; ``delegation_edges`` records one entry
    # per delegating function. Defaults keep every existing construction site
    # (tests, stubs) valid: a non-proxy contract is ``is_proxy=False`` with an
    # empty ``delegation_edges`` list.
    is_proxy: bool = False
    delegation_edges: list[DelegationEdge] = field(default_factory=list)


@dataclass
class Stage1Table:
    """Complete Stage 1 output for a project."""
    contracts: dict[str, FirstPartyContract] = field(default_factory=dict)
    project_path: str = ""

    def to_json(self) -> dict:
        return {
            "project_path": self.project_path,
            "contracts": {name: asdict(c) for name, c in self.contracts.items()},
        }

    def to_text(self) -> str:
        """Human-readable compact table."""
        lines = []
        for cname, contract in self.contracts.items():
            lines.append(f"=== Contract: {cname} ===")
            lines.append(f"Kind: {contract.kind}")
            lines.append(f"Source: {contract.source_file}")
            if getattr(contract, "is_proxy", False):
                lines.append("Proxy: yes (delegates calls to a state-held address)")
            lines.append("")

            # State variables
            if contract.state_vars:
                lines.append("State Variables (writer = function that writes):")
                for sv in contract.state_vars:
                    writers_str = ", ".join(sv.writers) if sv.writers else "(none)"
                    const_flag = " constant" if sv.is_constant else ""
                    immut_flag = " immutable" if sv.is_immutable else ""
                    lines.append(f"  {sv.name} : {sv.type}{const_flag}{immut_flag} | writers: {writers_str}")
                lines.append("")

            # Function gates
            if contract.function_gates:
                lines.append("External/Public Functions (access gate):")
                for fg in contract.function_gates:
                    constr = " constructor" if fg.is_constructor else ""
                    fb = " fallback" if fg.is_fallback else ""
                    rcv = " receive" if fg.is_receive else ""
                    lines.append(f"  {fg.name}{fg.signature} : {fg.modifier}{constr}{fb}{rcv}")
                lines.append("")

            # Caller graph
            if contract.caller_edges:
                lines.append("First-Party Caller Graph (drops OZ/libraries):")
                for edge in contract.caller_edges:
                    lines.append(f"  {edge.caller_contract}.{edge.caller_function} -> {edge.callee_contract}.{edge.callee_function} [{edge.call_type}]")
                lines.append("")

            # Delegation edges (proxy pattern)
            if getattr(contract, "delegation_edges", None):
                lines.append("Delegation Edges (delegatecall to state-held target):")
                for dedge in contract.delegation_edges:
                    lines.append(
                        f"  {dedge.proxy_contract}.{dedge.delegating_function} "
                        f"=> target[{dedge.target_state_var}]"
                    )
                lines.append("")

        return "\n".join(lines)


def _ordered_contract(fpc: FirstPartyContract) -> FirstPartyContract:
    """Return ``fpc`` with its per-contract collections deterministically sorted.

    Pure, slither-free helper so ordering can be tested in isolation. Sorting
    keys use ascending Unicode code point (Python's default tuple/str ordering)
    so the result is stable regardless of ``PYTHONHASHSEED`` (Requirement 21.6):

    * ``state_vars``    sorted by variable name.
    * ``function_gates`` sorted by signature (name + params).
    * ``caller_edges``   sorted by the stable tuple
      (caller_contract, caller_function, callee_contract, callee_function,
      call_type).

    The dataclass shapes and field names are left unchanged; only ordering of
    the list fields is normalized. Mutates and returns the same instance.
    """
    fpc.state_vars.sort(key=lambda sv: sv.name)
    fpc.function_gates.sort(key=lambda fg: fg.name + fg.signature)
    fpc.caller_edges.sort(
        key=lambda e: (
            e.caller_contract,
            e.caller_function,
            e.callee_contract,
            e.callee_function,
            e.call_type,
        )
    )
    # Delegation edges sorted by the stable tuple so serialized output is stable
    # regardless of discovery order (Requirement 21.6).
    fpc.delegation_edges.sort(
        key=lambda d: (
            d.proxy_contract,
            d.delegating_function,
            d.target_state_var,
        )
    )
    return fpc


def _library_scope(source_file: str, dep_roots: list[Path]) -> str:
    """Classify a library's scope from its source location (Requirement 19.5).

    Returns ``"dependency"`` when ``source_file`` lives under any resolved
    dependency root (``node_modules`` / ``lib`` / a configured deps root), and
    ``"first_party"`` otherwise. Dependency-scope libraries are excluded from the
    Stage 1 table, so their functions never reach the methods block; first-party
    libraries (declared in the project's own source) are kept.

    Pure and slither-free: it reasons only about paths, so its behavior is
    testable with temp-directory fixtures without any analyzer.

    Args:
        source_file: The library's declaring source file (absolute or relative).
        dep_roots: Resolved dependency roots from the Dependency_Resolver.

    Returns:
        ``"dependency"`` or ``"first_party"``.
    """
    if not source_file:
        # No source location: treat as first-party by default (never filtered on
        # a missing path, consistent with the extractor's fall-through behavior).
        return "first_party"

    try:
        src = Path(source_file).resolve()
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        src = Path(source_file)

    for root in dep_roots:
        try:
            resolved_root = Path(root).resolve()
        except (OSError, RuntimeError):  # pragma: no cover - defensive
            resolved_root = Path(root)
        if src == resolved_root or resolved_root in src.parents:
            return "dependency"
    return "first_party"


# LowLevelCall function names that denote a delegatecall (Requirement 19.4).
_DELEGATECALL_NAMES = frozenset({"delegatecall"})


def _detect_delegation(
    contract_name: str,
    functions: list,
) -> tuple[bool, list[DelegationEdge]]:
    """Detect proxy delegation from a contract's function call metadata (R19.4).

    Scans each function's ``external_calls`` for a delegatecall (a low-level
    call whose ``function_name`` is ``delegatecall``). When found, the contract
    is a proxy and one :class:`DelegationEdge` is recorded per delegating
    function, naming the state variable that holds the delegation target when a
    single state variable is read by that function, or ``"unresolved"`` when no
    single state var holds it.

    The heuristic for the target state var: if the delegating function reads
    exactly one state variable, that variable is taken as the target holder;
    otherwise the target is ``"unresolved"``. This is deliberately conservative -
    a false ``unresolved`` is safer than naming the wrong variable.

    Pure and duck-typed: ``functions`` is any iterable of objects exposing
    ``name``, ``external_calls`` (list of dicts with ``function_name``), and
    ``state_vars_read`` (list of names). This lets it be unit-tested with simple
    stand-in objects, no slither required.

    Args:
        contract_name: The name of the (candidate proxy) contract.
        functions: The contract's functions (duck-typed, see above).

    Returns:
        ``(is_proxy, delegation_edges)``. ``is_proxy`` is False with an empty
        list when no delegatecall is present.
    """
    edges: list[DelegationEdge] = []
    for func in functions:
        external_calls = getattr(func, "external_calls", []) or []
        delegates = any(
            (ec.get("function_name", "") or "").lower() in _DELEGATECALL_NAMES
            for ec in external_calls
            if isinstance(ec, dict)
        )
        if not delegates:
            continue

        reads = list(getattr(func, "state_vars_read", []) or [])
        target = reads[0] if len(reads) == 1 else "unresolved"
        edges.append(
            DelegationEdge(
                proxy_contract=contract_name,
                delegating_function=getattr(func, "name", ""),
                target_state_var=target,
            )
        )

    return (len(edges) > 0), edges


class FirstPartyExtractor:
    """
    Extracts first-party contracts from a Solidity project using slither.
    Filters out OZ, libraries, and shared_deps contracts.
    """

    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path).resolve()
        self.analyzer = SolidityAnalyzer(self.project_path)
        self.graph: Optional[SolidityGraph] = None

        # Identify first-party contracts (defined in project, not in shared_deps)
        self.first_party_names: set[str] = set()
        self.contract_to_file: dict[str, str] = {}

    def analyze(self) -> Stage1Table:
        """Run full analysis and build Stage 1 table.

        Raises:
            CompileError: When slither/solc cannot compile the project. The
                error carries the compiler diagnostics, the attempted solc
                version, and the applied remappings so the orchestrator can
                report the ``compile_failed`` outcome (Requirement 19.6).
        """
        print(f"Analyzing {self.project_path}...")
        try:
            self.graph = self.analyzer.analyze()
        except CompileError:
            raise
        except Exception as exc:  # slither/crytic-compile compile failure
            raise CompileError(
                diagnostics=str(exc),
                solc_version=os.environ.get("SOLC_VERSION", "unknown"),
                remappings=self._attempted_remappings(),
            ) from exc

        # Identify first-party contracts
        self._identify_first_party()

        # Build table.
        #
        # Iterate first-party names in sorted order (ascending Unicode code
        # point) so the contract set is populated deterministically regardless
        # of PYTHONHASHSEED, which randomizes set iteration order per process
        # (Requirement 21.6). Insertion order is preserved by dict, so the
        # serialized Stage1Table is byte-identical across processes.
        table = Stage1Table(project_path=str(self.project_path))
        for cname in sorted(self.first_party_names):
            if cname in self.graph.contracts:
                cinfo = self.graph.contracts[cname]
                fpc = _ordered_contract(self._build_first_party_contract(cinfo))
                table.contracts[cname] = fpc

        print(f"Found {len(table.contracts)} first-party contracts (filtered from {len(self.graph.contracts)} total)")
        return table

    def _identify_first_party(self) -> None:
        """Determine which contracts are first-party (defined in project source files)."""
        if not self.graph:
            return

        # Find the actual project root (not just the file's parent)
        # Look for foundry.toml, hardhat.config.js, package.json, or contracts/ directory
        project_root = self._find_project_root(self.project_path)

        # Resolve shared dependency roots via the Dependency_Resolver (replaces the
        # former hardcoded analyzer._SHARED_DEPS). A source file under ANY resolved
        # dependency root is dependency scope, not first-party (Requirements 7.2,
        # 19.5). Compute once here. Imported lazily: resolve.py is pure (no slither
        # import), and a lazy import avoids triggering the eager spec_pipeline
        # package __init__ when stage1_extract is loaded in isolation by tests.
        from spec_pipeline.resolve import Dependency_Resolver

        dep_roots = [r for r in Dependency_Resolver().resolve(self.project_path).roots]

        def _under_dep_root(path: Path) -> bool:
            parents = set(path.parents)
            return any(root in parents or path == root for root in dep_roots)

        for cname, cinfo in self.graph.contracts.items():
            src_file = cinfo.source_file
            if not src_file:
                # ponytail: empty source_file = compiler artifact or abstract — skip, not first-party
                continue

            src_path = Path(src_file)
            try:
                # Resolve relative to project
                if src_path.is_absolute():
                    # Check if under a dependency root (OZ/library - skip)
                    if _under_dep_root(src_path):
                        continue
                    # Check if under project root
                    if project_root and project_root in src_path.parents:
                        self.first_party_names.add(cname)
                        self.contract_to_file[cname] = str(src_path)
                    elif src_path == project_root:
                        self.first_party_names.add(cname)
                        self.contract_to_file[cname] = str(src_path)
                    else:
                        # Outside both - could be imported from elsewhere, check if it looks like project source
                        # If it's under the same top-level directory as project_root, consider it first-party
                        if project_root:
                            try:
                                # Share a common ancestor that is not a dependency root.
                                src_parents = set(src_path.parents)
                                proj_parents = set(project_root.parents)
                                common = src_parents & proj_parents
                                if common and not any(root in common for root in dep_roots):
                                    self.first_party_names.add(cname)
                                    self.contract_to_file[cname] = str(src_path)
                            except Exception:
                                pass
                else:
                    # Relative path - likely first-party
                    self.first_party_names.add(cname)
                    self.contract_to_file[cname] = str(src_path)
            except Exception:
                # Default to first-party on error
                self.first_party_names.add(cname)
                self.contract_to_file[cname] = src_file

    def _attempted_remappings(self) -> list[str]:
        """Return the solc remappings applied for the compile attempt (R19.6).

        Best-effort: computed via the same ``_build_solc_remaps`` the analyzer
        uses, so a :class:`CompileError` can report what was applied. Returns an
        empty list on any failure (the diagnostics remain the primary signal).
        """
        try:
            remaps = _build_solc_remaps(self.project_path)
        except Exception:  # pragma: no cover - defensive
            return []
        if not remaps:
            return []
        return remaps.split() if isinstance(remaps, str) else list(remaps)

    def _find_project_root(self, path: Path) -> Path | None:
        """Find the project root directory by looking for marker files/directories."""
        # Start from the given path (file or directory)
        if path.is_file():
            current = path.parent
        else:
            current = path

        # Walk up the tree looking for project markers
        for parent in [current] + list(current.parents):
            # Check for common project markers
            if (parent / "foundry.toml").exists():
                return parent
            if (parent / "hardhat.config.js").exists() or (parent / "hardhat.config.ts").exists():
                return parent
            if (parent / "package.json").exists():
                return parent
            if (parent / "contracts").is_dir():
                return parent
            if (parent / "src").is_dir():
                return parent

        # Fallback: if we're in a Raw_Scraper repo, go up to the contracts directory
        # This handles the certora-dataset layout
        for parent in [current] + list(current.parents):
            if parent.name == "contracts" and parent.parent.name.endswith("-contracts"):
                return parent

        # Ultimate fallback: return the current directory
        return current

    def _build_first_party_contract(self, cinfo: ContractInfo) -> FirstPartyContract:
        """Build compact FirstPartyContract from full ContractInfo."""
        fpc = FirstPartyContract(
            name=cinfo.name,
            kind=cinfo.kind,
            source_file=cinfo.source_file,
        )

        # 1. State variables with writers
        # Build reverse mapping: state_var -> list of functions that write it
        writers_map: dict[str, list[str]] = defaultdict(list)
        for func in cinfo.functions:
            for svar in func.state_vars_written:
                writers_map[svar].append(func.name)

        for svar in cinfo.state_variables:
            fpc.state_vars.append(StateVarInfo(
                name=svar["name"],
                type=svar["type"],
                visibility=svar["visibility"],
                is_constant=svar["is_constant"],
                is_immutable=svar["is_immutable"],
                writers=writers_map.get(svar["name"], []),
            ))

        # 2. Function gates (external/public only, with access modifier)
        for func in cinfo.functions:
            # Only include external/public functions and constructors
            if func.visibility in ("public", "external") or func.is_constructor or func.is_fallback or func.is_receive:
                # Build signature
                params = ", ".join(f"{p['type']} {p['name']}" for p in func.parameters)
                signature = f"({params})"
                if func.return_types:
                    signature += f" -> {', '.join(func.return_types)}"

                # Find the access-control modifier (exclude non-access ones)
                access_mod = "none"
                for mod in func.modifiers:
                    mod_lower = mod.lower()
                    if any(kw in mod_lower for kw in ["only", "auth", "role", "owner", "admin", "permission", "access", "authorized"]):
                        access_mod = mod
                        break
                if access_mod == "none" and func.modifiers:
                    # Use first modifier if no obvious access control
                    access_mod = func.modifiers[0]

                fpc.function_gates.append(FunctionGate(
                    name=func.name,
                    signature=signature,
                    visibility=func.visibility,
                    mutability=func.mutability,
                    modifier=access_mod,
                    is_constructor=func.is_constructor,
                    is_fallback=func.is_fallback,
                    is_receive=func.is_receive,
                ))

        # 3. First-party caller edges
        for func in cinfo.functions:
            # Internal calls (same contract)
            for callee_name in func.internal_calls:
                if callee_name in [f.name for f in cinfo.functions]:
                    fpc.caller_edges.append(CallerEdge(
                        caller_contract=cinfo.name,
                        caller_function=func.name,
                        callee_contract=cinfo.name,
                        callee_function=callee_name,
                        call_type="internal",
                    ))

            # External calls to first-party contracts
            for ext_call in func.external_calls:
                target = ext_call.get("target", "")
                fn_name = ext_call.get("function_name", "")
                call_type = ext_call.get("call_type", "")

                # Check if target is a first-party contract
                target_contract = self._resolve_target_contract(target, fn_name)
                if target_contract and target_contract in self.first_party_names:
                    fpc.caller_edges.append(CallerEdge(
                        caller_contract=cinfo.name,
                        caller_function=func.name,
                        callee_contract=target_contract,
                        callee_function=fn_name,
                        call_type="external",
                    ))

        # 4. Proxy detection: mark the contract as a proxy and record one
        #    delegation edge per delegating function when it delegatecalls to a
        #    state-held address (Requirement 19.4).
        is_proxy, delegation_edges = _detect_delegation(cinfo.name, cinfo.functions)
        fpc.is_proxy = is_proxy
        fpc.delegation_edges = delegation_edges

        return fpc

    def _resolve_target_contract(self, target: str, fn_name: str) -> Optional[str]:
        """Resolve external call target to a first-party contract name."""
        # Direct match
        if target in self.first_party_names:
            return target

        # Case-insensitive match against first-party only
        for name in self.first_party_names:
            if name.lower() == target.lower():
                return name

        # ponytail: no function-name fallback — false edges are worse than missing edges
        return None

    def export_json(self, table: Stage1Table, output_path: str | Path) -> None:
        """Export the Stage 1 table's canonical Artifact_Store payload as JSON.

        The JSON payload is produced by
        :func:`spec_pipeline.artifacts.serialize_stage1`, so this sidecar emits
        exactly the ``payload`` the Artifact_Store round-trips (Requirement 2.5)
        rather than an independent ``table.to_json()`` that could drift from the
        store's canonical form. The canonical Stage 1 envelope
        (``{provenance, payload}``) remains the orchestrator's responsibility via
        ``_write_stage1_envelope``; this method writes the bare payload only, so
        there is a single source of the envelope and no double/conflicting writes.

        The bytes match the store's ``_canonical_json`` convention
        (Requirement 21.7): keys sorted at every depth, two-space indentation,
        ``\\n`` line endings, non-ASCII preserved (``ensure_ascii=False``), and
        exactly one trailing newline.

        ``serialize_stage1`` is imported lazily here: ``artifacts.py`` imports the
        Stage 1 dataclasses lazily to stay slither-free, so a module-top import
        of ``serialize_stage1`` into this module risks an import cycle. A local
        import inside the method avoids it.
        """
        from spec_pipeline.artifacts import serialize_stage1

        payload = serialize_stage1(table)
        text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
        Path(output_path).write_text(text + "\n", encoding="utf-8", newline="")

    def export_text(self, table: Stage1Table, output_path: str | Path) -> None:
        """Export Stage 1 table as human-readable text."""
        with open(output_path, "w") as f:
            f.write(table.to_text())


def extract_first_party(
    path: str | Path,
    output_dir: str | Path | None = None,
) -> Stage1Table:
    """
    Run Stage 1 extraction on a Solidity file or directory.

    Args:
        path: Path to .sol file or directory
        output_dir: Output directory (default: same as input)

    Returns:
        Stage1Table with first-party contracts only
    """
    path = Path(path).resolve()

    if output_dir is None:
        output_dir = path if path.is_dir() else path.parent
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    extractor = FirstPartyExtractor(path)
    table = extractor.analyze()

    # Export
    base_name = path.stem if path.is_file() else path.name
    extractor.export_json(table, output_dir / f"{base_name}_stage1.json")
    extractor.export_text(table, output_dir / f"{base_name}_stage1.txt")

    print(f"Exported Stage 1 to {output_dir / f'{base_name}_stage1.json'} and .txt")
    return table