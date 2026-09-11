"""
Artifact_Store - single owner of all on-disk stage state (design: R2, R3, R21).

This module centralizes how the pipeline names, fingerprints, serializes, and
deserializes its per-stage artifacts. Concentrating that logic here removes the
current inconsistency where directory inputs write ``{dir.name}_stage1.json``
while the loaders in ``pipeline.py`` read ``project_stage1.json`` and hand later
stages an empty table.

This file (task 2.1) implements only the two foundational, side-effect-free
helpers that every other piece of the store builds on:

* :func:`artifact_base_name` - the ONE base-name function every stage calls for
  both reads and writes, so the write name always equals the read name for both
  file inputs and directory inputs (Requirement 2.5).
* :func:`source_fingerprint` - a deterministic SHA-256 digest over the analyzed
  first-party ``.sol`` files, used to detect stale artifacts (Requirement 3.2).

Task 2.2 adds the canonical envelope layer on top of those helpers:

* :class:`Provenance` - the per-artifact provenance record (Requirement 3.1).
* :class:`ArtifactError` - raised on schema/type/JSON faults (Requirements 2.6, 2.8).
* :class:`LoadResult` - the typed result of :func:`load_artifact`.
* :func:`write_artifact` / :func:`load_artifact` - canonical envelope writer and
  loader (Requirements 2.7, 3.1, 21.7).

The remaining pieces of the Artifact_Store are implemented in later tasks and
are intentionally NOT defined here:

* ``serialize_stage1`` / ``deserialize_stage1`` - Stage 1 typed (de)serialization
  (task 2.3).
* ``is_stale`` - staleness detection (task 2.6).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "artifact_base_name",
    "source_fingerprint",
    "Provenance",
    "ArtifactError",
    "LoadResult",
    "write_artifact",
    "load_artifact",
    "serialize_stage1",
    "deserialize_stage1",
]


def artifact_base_name(input_path: Path) -> str:
    """Return the single artifact base name shared by every stage of one run.

    This is the ONE function every stage MUST call to derive the artifact prefix
    for both reads and writes, so the name used to write an artifact always
    equals the name used to read it (Requirement 2.5).

    Naming rule:

    * File input  -> the file name with a single trailing ``.sol`` removed
      (e.g. ``Pool.sol`` -> ``Pool``). Other suffixes are left untouched.
    * Directory input -> the directory name unchanged (e.g. ``aave-v3-core`` ->
      ``aave-v3-core``).

    The decision is based on the path's spelling, not on whether it exists on
    disk: a name ending in ``.sol`` is treated as a file, everything else as a
    directory. This keeps the function pure and usable for both the write path
    (where the input exists) and later reasoning about names.

    Args:
        input_path: The analyzed source path (a ``.sol`` file or a directory).

    Returns:
        The base name to use as the artifact prefix.
    """
    path = Path(input_path)
    name = path.name
    if name.endswith(".sol"):
        # Strip exactly one trailing ".sol"; leave any other dotted suffix.
        return name[: -len(".sol")]
    return name


def source_fingerprint(analyzed_root: Path, first_party_sol: list[Path]) -> str:
    """Compute a stable SHA-256 fingerprint of the analyzed first-party sources.

    The fingerprint is derived from the sorted list of
    ``(relative_path, sha256(file_content))`` pairs, where each path is taken
    relative to *analyzed_root* (Requirement 3.2). Dependency-root files
    (``node_modules``, ``lib``, ...) are excluded by the caller passing only
    first-party ``.sol`` files, so a change under a dependency root never alters
    the fingerprint.

    Determinism is guaranteed independent of OS and of the input list order:

    * Relative paths are normalized with :meth:`Path.as_posix` so separators do
      not depend on the platform.
    * The ``(relpath, digest)`` pairs are sorted lexicographically before
      hashing, so a shuffled *first_party_sol* yields the same result.

    Args:
        analyzed_root: The root the file paths are made relative to.
        first_party_sol: The first-party ``.sol`` files to include in the digest.

    Returns:
        A stable hex digest string prefixed with ``"sha256:"``.
    """
    analyzed_root = Path(analyzed_root)

    pairs: list[tuple[str, str]] = []
    for sol_path in first_party_sol:
        sol_path = Path(sol_path)
        try:
            rel = sol_path.resolve().relative_to(analyzed_root.resolve())
            rel_str = rel.as_posix()
        except ValueError:
            # File is not under analyzed_root; fall back to its posix name so the
            # entry is still deterministic rather than raising.
            rel_str = sol_path.as_posix()

        content = sol_path.read_bytes()
        content_digest = hashlib.sha256(content).hexdigest()
        pairs.append((rel_str, content_digest))

    pairs.sort()

    digest = hashlib.sha256()
    for rel_str, content_digest in pairs:
        # NUL separators keep the encoding unambiguous across entries.
        digest.update(rel_str.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content_digest.encode("ascii"))
        digest.update(b"\x00")

    return f"sha256:{digest.hexdigest()}"


# ---------------------------------------------------------------------------
# Provenance envelope (task 2.2) - Requirements 2.6, 2.7, 3.1, 21.7
# ---------------------------------------------------------------------------


class ArtifactError(Exception):
    """Raised when an artifact is malformed on disk.

    Two fault classes raise this error, each naming enough context for an
    operator to locate the problem:

    * A required provenance field is missing or carries the wrong type. The
      message names the artifact path, the contract (when the offending field
      belongs to a contract), and the offending field (Requirement 2.6).
    * The file content is not valid JSON. The message names the artifact path
      and the JSON parse position (Requirement 2.8).
    """


@dataclass
class Provenance:
    """Provenance recorded alongside every Stage_Artifact (Requirement 3.1).

    Attributes:
        pipeline_version: The Spec_Pipeline version that produced the artifact.
        stage: The producing stage number.
        completed_utc: ISO-8601 UTC completion time (whole-second or finer).
        source_path: The resolved analyzed source path.
        source_fingerprint: The :func:`source_fingerprint` of the analyzed sources.
        consumed: ``(stage, sha256)`` pairs for each consumed input artifact;
            an empty list when the stage consumed no artifacts.
    """

    pipeline_version: str
    stage: int
    completed_utc: str
    source_path: str
    source_fingerprint: str
    consumed: list[tuple[int, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict form suitable for canonical JSON serialization."""
        return {
            "pipeline_version": self.pipeline_version,
            "stage": self.stage,
            "completed_utc": self.completed_utc,
            "source_path": self.source_path,
            "source_fingerprint": self.source_fingerprint,
            # tuples are serialized as JSON arrays [stage, sha256]
            "consumed": [[int(s), str(d)] for (s, d) in self.consumed],
        }

    @classmethod
    def from_dict(cls, data: Any, *, artifact_path: Path) -> "Provenance":
        """Reconstruct a :class:`Provenance` from a parsed envelope section.

        Raises:
            ArtifactError: If the provenance object is missing a required field
                or a field carries the wrong type (Requirement 2.6).
        """
        if not isinstance(data, dict):
            raise ArtifactError(
                f"{artifact_path}: 'provenance' must be an object, "
                f"got {type(data).__name__}"
            )

        def _require(name: str, expected: type, type_label: str) -> Any:
            if name not in data:
                raise ArtifactError(
                    f"{artifact_path}: missing required provenance field '{name}'"
                )
            value = data[name]
            # bool is a subclass of int; reject it explicitly for int fields.
            if expected is int and isinstance(value, bool):
                raise ArtifactError(
                    f"{artifact_path}: provenance field '{name}' must be "
                    f"{type_label}, got bool"
                )
            if not isinstance(value, expected):
                raise ArtifactError(
                    f"{artifact_path}: provenance field '{name}' must be "
                    f"{type_label}, got {type(value).__name__}"
                )
            return value

        pipeline_version = _require("pipeline_version", str, "a string")
        stage = _require("stage", int, "an integer")
        completed_utc = _require("completed_utc", str, "a string")
        source_path = _require("source_path", str, "a string")
        source_fingerprint = _require("source_fingerprint", str, "a string")
        consumed_raw = _require("consumed", list, "a list")

        consumed: list[tuple[int, str]] = []
        for index, entry in enumerate(consumed_raw):
            field_name = f"consumed[{index}]"
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise ArtifactError(
                    f"{artifact_path}: provenance field '{field_name}' must be "
                    f"a [stage, sha256] pair"
                )
            entry_stage, entry_digest = entry
            if isinstance(entry_stage, bool) or not isinstance(entry_stage, int):
                raise ArtifactError(
                    f"{artifact_path}: provenance field '{field_name}' stage "
                    f"must be an integer"
                )
            if not isinstance(entry_digest, str):
                raise ArtifactError(
                    f"{artifact_path}: provenance field '{field_name}' digest "
                    f"must be a string"
                )
            consumed.append((entry_stage, entry_digest))

        return cls(
            pipeline_version=pipeline_version,
            stage=stage,
            completed_utc=completed_utc,
            source_path=source_path,
            source_fingerprint=source_fingerprint,
            consumed=consumed,
        )


@dataclass
class LoadResult:
    """Typed result of :func:`load_artifact`.

    Attributes:
        present: Whether the artifact file existed on disk. An absent file is
            reported as ``present=False`` and is NOT an error.
        payload: The parsed ``payload`` object when present, else ``None``.
        provenance: The parsed :class:`Provenance` when present, else ``None``.
        contract_count: The number of contracts declared in a Stage 1 payload
            (a payload carrying a ``contracts`` mapping); ``None`` otherwise.
        error: Reserved for recoverable, non-raising load problems. Currently
            always ``None`` (schema/JSON faults raise :class:`ArtifactError`).
    """

    present: bool
    payload: Optional[dict] = None
    provenance: Optional[Provenance] = None
    contract_count: Optional[int] = None
    error: Optional[str] = None


def _artifact_path(output_dir: Path, base: str, stage: int) -> Path:
    """Return the canonical artifact path ``{base}_stage{stage}.json``."""
    return Path(output_dir) / f"{base}_stage{stage}.json"


def _canonical_json(obj: Any) -> str:
    """Serialize *obj* as canonical JSON with exactly one trailing newline.

    Canonical form (Requirement 21.7): keys sorted at every nesting depth,
    two-space indentation, ``\\n`` (LF) line endings, non-ASCII preserved
    (UTF-8), and exactly one trailing newline.
    """
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)
    return text + "\n"


def write_artifact(
    output_dir: Path,
    base: str,
    stage: int,
    payload: dict,
    prov: Provenance,
) -> Path:
    """Write a ``{provenance, payload}`` envelope as canonical JSON.

    The envelope is written to ``{base}_stage{stage}.json`` under *output_dir*
    with sorted keys at every depth, two-space indentation, LF line endings,
    exactly one trailing newline, and UTF-8 encoding (Requirement 21.7).

    Args:
        output_dir: Directory the artifact is written into (created if absent).
        base: Artifact base name from :func:`artifact_base_name`.
        stage: The producing stage number.
        payload: The stage-specific payload object.
        prov: The provenance to record.

    Returns:
        The path the artifact was written to.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = _artifact_path(output_dir, base, stage)

    envelope = {
        "provenance": prov.to_dict(),
        "payload": payload,
    }
    # Write bytes explicitly as UTF-8 with LF endings; newline="" prevents any
    # platform-specific newline translation.
    path.write_text(_canonical_json(envelope), encoding="utf-8", newline="")
    return path


def load_artifact(output_dir: Path, base: str, stage: int) -> LoadResult:
    """Load and validate a ``{base}_stage{stage}.json`` envelope.

    On a well-formed envelope this returns ``present=True`` with the parsed
    payload and provenance. When the payload is a Stage 1 artifact (it carries
    a ``contracts`` mapping) the ``contract_count`` is computed from it; it is
    ``None`` for other stages (Requirement 2.7).

    An absent file is reported as ``present=False`` and is NOT an error.

    Args:
        output_dir: Directory the artifact is read from.
        base: Artifact base name from :func:`artifact_base_name`.
        stage: The producing stage number.

    Returns:
        A :class:`LoadResult`.

    Raises:
        ArtifactError: If the file is not valid JSON (names the path and the
            parse position, Requirement 2.8), or if the envelope shape or a
            provenance field is missing/wrong-typed (names the offending field,
            Requirement 2.6).
    """
    path = _artifact_path(output_dir, base, stage)

    if not path.exists():
        return LoadResult(present=False)

    raw = path.read_text(encoding="utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactError(
            f"{path}: invalid JSON at line {exc.lineno} column {exc.colno} "
            f"(char {exc.pos}): {exc.msg}"
        ) from exc

    if not isinstance(envelope, dict):
        raise ArtifactError(
            f"{path}: artifact root must be an object, "
            f"got {type(envelope).__name__}"
        )
    if "provenance" not in envelope:
        raise ArtifactError(f"{path}: missing required field 'provenance'")
    if "payload" not in envelope:
        raise ArtifactError(f"{path}: missing required field 'payload'")

    provenance = Provenance.from_dict(envelope["provenance"], artifact_path=path)

    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise ArtifactError(
            f"{path}: 'payload' must be an object, got {type(payload).__name__}"
        )

    contract_count: Optional[int] = None
    contracts = payload.get("contracts")
    if isinstance(contracts, dict):
        contract_count = len(contracts)

    return LoadResult(
        present=True,
        payload=payload,
        provenance=provenance,
        contract_count=contract_count,
        error=None,
    )


# ---------------------------------------------------------------------------
# Stage 1 typed (de)serialization (task 2.3) - Requirements 2.1, 2.2, 2.6
# ---------------------------------------------------------------------------
#
# This pair owns the round-trip contract for the Stage 1 table. It is the fix
# for the empty-table defect: today ``pipeline._load_stage1`` returns an empty
# ``Stage1Table`` even when a real artifact exists on disk, so stages 2+ analyze
# zero contracts. ``deserialize_stage1`` reconstructs the full typed table.
#
# The Stage 1 dataclasses live in ``spec_pipeline.stage1_extract``, whose module
# import eagerly pulls in ``solidity_graph.analyzer`` (and thus slither). To keep
# ``artifacts.py`` importable without the external toolchain, the dataclasses are
# imported LAZILY inside these functions rather than at module load.


def _import_stage1_types() -> tuple:
    """Lazily import the Stage 1 dataclasses (keeps artifacts.py slither-free).

    Returns the tuple ``(Stage1Table, FirstPartyContract, StateVarInfo,
    FunctionGate, CallerEdge)``. Imported inside the function body so that
    importing :mod:`spec_pipeline.artifacts` never triggers the slither-backed
    ``solidity_graph.analyzer`` import chain.
    """
    from spec_pipeline.stage1_extract import (
        CallerEdge,
        DelegationEdge,
        FirstPartyContract,
        FunctionGate,
        Stage1Table,
        StateVarInfo,
    )

    return (
        Stage1Table,
        FirstPartyContract,
        StateVarInfo,
        FunctionGate,
        CallerEdge,
        DelegationEdge,
    )


def serialize_stage1(table: "Any") -> dict:
    """Serialize a :class:`Stage1Table` to a plain ``dict`` (Requirement 2.1).

    Writes EVERY field of the table and of each contained ``FirstPartyContract``,
    ``StateVarInfo``, ``FunctionGate``, and ``CallerEdge`` so that the artifact is
    lossless. The emitted shape is exactly ``Stage1Table.to_json()`` (which uses
    ``dataclasses.asdict`` and therefore already emits every field), but the
    mapping is spelled out here field by field so this module owns the round-trip
    contract independent of any future change to ``to_json``.

    Args:
        table: The :class:`Stage1Table` to serialize.

    Returns:
        A JSON-serializable ``dict`` with ``project_path`` and a ``contracts``
        mapping of contract name to its fully expanded fields.
    """
    contracts: dict[str, Any] = {}
    for name, contract in table.contracts.items():
        contracts[name] = {
            "name": contract.name,
            "kind": contract.kind,
            "source_file": contract.source_file,
            "state_vars": [
                {
                    "name": sv.name,
                    "type": sv.type,
                    "visibility": sv.visibility,
                    "is_constant": sv.is_constant,
                    "is_immutable": sv.is_immutable,
                    "writers": list(sv.writers),
                }
                for sv in contract.state_vars
            ],
            "function_gates": [
                {
                    "name": fg.name,
                    "signature": fg.signature,
                    "visibility": fg.visibility,
                    "mutability": fg.mutability,
                    "modifier": fg.modifier,
                    "is_constructor": fg.is_constructor,
                    "is_fallback": fg.is_fallback,
                    "is_receive": fg.is_receive,
                }
                for fg in contract.function_gates
            ],
            "caller_edges": [
                {
                    "caller_contract": edge.caller_contract,
                    "caller_function": edge.caller_function,
                    "callee_contract": edge.callee_contract,
                    "callee_function": edge.callee_function,
                    "call_type": edge.call_type,
                }
                for edge in contract.caller_edges
            ],
            # R19.4: proxy marking + delegation edges. Emitted here so the
            # artifact is lossless and matches ``Stage1Table.to_json()``
            # (asdict), keeping the round-trip property green.
            "is_proxy": contract.is_proxy,
            "delegation_edges": [
                {
                    "proxy_contract": dedge.proxy_contract,
                    "delegating_function": dedge.delegating_function,
                    "target_state_var": dedge.target_state_var,
                }
                for dedge in contract.delegation_edges
            ],
        }

    return {
        "project_path": table.project_path,
        "contracts": contracts,
    }


def _stage1_field(
    data: dict,
    name: str,
    expected: type,
    type_label: str,
    *,
    context: str,
    contract: Optional[str] = None,
) -> Any:
    """Read a required field from *data*, raising :class:`ArtifactError` on fault.

    The error message names the artifact *context*, the *contract* (when the
    field belongs to a contract or a nested value), and the offending *name*
    (Requirement 2.6, consistent with the R2.6 message style).
    """
    where = f"{context}"
    if contract is not None:
        where += f" contract '{contract}'"

    if name not in data:
        raise ArtifactError(f"{where}: missing required field '{name}'")

    value = data[name]
    # bool is a subclass of int; reject it explicitly when an int/str is wanted.
    if expected is bool:
        if not isinstance(value, bool):
            raise ArtifactError(
                f"{where}: field '{name}' must be {type_label}, "
                f"got {type(value).__name__}"
            )
    elif expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ArtifactError(
                f"{where}: field '{name}' must be {type_label}, "
                f"got {type(value).__name__}"
            )
    elif not isinstance(value, expected):
        raise ArtifactError(
            f"{where}: field '{name}' must be {type_label}, "
            f"got {type(value).__name__}"
        )
    return value


def deserialize_stage1(payload: dict, *, context: str = "stage1 artifact") -> "Any":
    """Reconstruct a typed :class:`Stage1Table` from *payload* (Requirement 2.2).

    Rebuilds the table and every nested ``FirstPartyContract``, ``StateVarInfo``,
    ``FunctionGate``, and ``CallerEdge`` with typed values. This is the fix for
    the empty-table defect: it never returns an empty table when the payload
    declares contracts.

    Args:
        payload: The serialized Stage 1 payload (as produced by
            :func:`serialize_stage1`).
        context: A label naming the artifact for error messages (e.g. the
            artifact path). Defaults to ``"stage1 artifact"``.

    Returns:
        A fully typed :class:`Stage1Table`.

    Raises:
        ArtifactError: If a declared field is missing or carries a value whose
            type differs from the declared type. The message names the artifact
            context, the contract, and the offending field (Requirement 2.6).
    """
    (
        Stage1Table,
        FirstPartyContract,
        StateVarInfo,
        FunctionGate,
        CallerEdge,
        DelegationEdge,
    ) = _import_stage1_types()

    if not isinstance(payload, dict):
        raise ArtifactError(
            f"{context}: payload must be an object, got {type(payload).__name__}"
        )

    project_path = _stage1_field(
        payload, "project_path", str, "a string", context=context
    )
    contracts_raw = _stage1_field(
        payload, "contracts", dict, "an object", context=context
    )

    table = Stage1Table(project_path=project_path)

    for cname, cdata in contracts_raw.items():
        if not isinstance(cdata, dict):
            raise ArtifactError(
                f"{context} contract '{cname}': contract entry must be an "
                f"object, got {type(cdata).__name__}"
            )

        name = _stage1_field(
            cdata, "name", str, "a string", context=context, contract=cname
        )
        kind = _stage1_field(
            cdata, "kind", str, "a string", context=context, contract=cname
        )
        source_file = _stage1_field(
            cdata, "source_file", str, "a string", context=context, contract=cname
        )

        state_vars_raw = _stage1_field(
            cdata, "state_vars", list, "a list", context=context, contract=cname
        )
        function_gates_raw = _stage1_field(
            cdata, "function_gates", list, "a list", context=context, contract=cname
        )
        caller_edges_raw = _stage1_field(
            cdata, "caller_edges", list, "a list", context=context, contract=cname
        )

        state_vars: list[Any] = []
        for sv in state_vars_raw:
            if not isinstance(sv, dict):
                raise ArtifactError(
                    f"{context} contract '{cname}': field 'state_vars' entry "
                    f"must be an object, got {type(sv).__name__}"
                )
            writers_raw = _stage1_field(
                sv, "writers", list, "a list", context=context, contract=cname
            )
            for w in writers_raw:
                if not isinstance(w, str):
                    raise ArtifactError(
                        f"{context} contract '{cname}': field "
                        f"'state_vars.writers' entry must be a string, "
                        f"got {type(w).__name__}"
                    )
            state_vars.append(
                StateVarInfo(
                    name=_stage1_field(
                        sv, "name", str, "a string", context=context, contract=cname
                    ),
                    type=_stage1_field(
                        sv, "type", str, "a string", context=context, contract=cname
                    ),
                    visibility=_stage1_field(
                        sv, "visibility", str, "a string", context=context,
                        contract=cname,
                    ),
                    is_constant=_stage1_field(
                        sv, "is_constant", bool, "a boolean", context=context,
                        contract=cname,
                    ),
                    is_immutable=_stage1_field(
                        sv, "is_immutable", bool, "a boolean", context=context,
                        contract=cname,
                    ),
                    writers=list(writers_raw),
                )
            )

        function_gates: list[Any] = []
        for fg in function_gates_raw:
            if not isinstance(fg, dict):
                raise ArtifactError(
                    f"{context} contract '{cname}': field 'function_gates' "
                    f"entry must be an object, got {type(fg).__name__}"
                )
            function_gates.append(
                FunctionGate(
                    name=_stage1_field(
                        fg, "name", str, "a string", context=context, contract=cname
                    ),
                    signature=_stage1_field(
                        fg, "signature", str, "a string", context=context,
                        contract=cname,
                    ),
                    visibility=_stage1_field(
                        fg, "visibility", str, "a string", context=context,
                        contract=cname,
                    ),
                    mutability=_stage1_field(
                        fg, "mutability", str, "a string", context=context,
                        contract=cname,
                    ),
                    modifier=_stage1_field(
                        fg, "modifier", str, "a string", context=context,
                        contract=cname,
                    ),
                    is_constructor=_stage1_field(
                        fg, "is_constructor", bool, "a boolean", context=context,
                        contract=cname,
                    ),
                    is_fallback=_stage1_field(
                        fg, "is_fallback", bool, "a boolean", context=context,
                        contract=cname,
                    ),
                    is_receive=_stage1_field(
                        fg, "is_receive", bool, "a boolean", context=context,
                        contract=cname,
                    ),
                )
            )

        # R19.4: proxy marking + delegation edges. Both are optional in the
        # payload so artifacts written before task 13.1 still deserialize:
        # ``is_proxy`` defaults to False and ``delegation_edges`` to []. When
        # present they are validated with the same field-typing rules.
        if "is_proxy" in cdata:
            is_proxy = _stage1_field(
                cdata, "is_proxy", bool, "a boolean", context=context,
                contract=cname,
            )
        else:
            is_proxy = False

        if "delegation_edges" in cdata:
            delegation_edges_raw = _stage1_field(
                cdata, "delegation_edges", list, "a list", context=context,
                contract=cname,
            )
        else:
            delegation_edges_raw = []

        caller_edges: list[Any] = []
        for edge in caller_edges_raw:
            if not isinstance(edge, dict):
                raise ArtifactError(
                    f"{context} contract '{cname}': field 'caller_edges' entry "
                    f"must be an object, got {type(edge).__name__}"
                )
            caller_edges.append(
                CallerEdge(
                    caller_contract=_stage1_field(
                        edge, "caller_contract", str, "a string", context=context,
                        contract=cname,
                    ),
                    caller_function=_stage1_field(
                        edge, "caller_function", str, "a string", context=context,
                        contract=cname,
                    ),
                    callee_contract=_stage1_field(
                        edge, "callee_contract", str, "a string", context=context,
                        contract=cname,
                    ),
                    callee_function=_stage1_field(
                        edge, "callee_function", str, "a string", context=context,
                        contract=cname,
                    ),
                    call_type=_stage1_field(
                        edge, "call_type", str, "a string", context=context,
                        contract=cname,
                    ),
                )
            )

        delegation_edges: list[Any] = []
        for dedge in delegation_edges_raw:
            if not isinstance(dedge, dict):
                raise ArtifactError(
                    f"{context} contract '{cname}': field 'delegation_edges' "
                    f"entry must be an object, got {type(dedge).__name__}"
                )
            delegation_edges.append(
                DelegationEdge(
                    proxy_contract=_stage1_field(
                        dedge, "proxy_contract", str, "a string", context=context,
                        contract=cname,
                    ),
                    delegating_function=_stage1_field(
                        dedge, "delegating_function", str, "a string",
                        context=context, contract=cname,
                    ),
                    target_state_var=_stage1_field(
                        dedge, "target_state_var", str, "a string",
                        context=context, contract=cname,
                    ),
                )
            )

        table.contracts[cname] = FirstPartyContract(
            name=name,
            kind=kind,
            source_file=source_file,
            state_vars=state_vars,
            function_gates=function_gates,
            caller_edges=caller_edges,
            is_proxy=is_proxy,
            delegation_edges=delegation_edges,
        )

    return table


# TODO(task 2.6): implement ``is_stale(prov, current_fingerprint,
# current_version, disk_inputs) -> Staleness | None`` here (Requirement 3.6).


# ---------------------------------------------------------------------------
# Staleness detection (task 2.6, Requirements 3.3, 3.4, 3.5)
# ---------------------------------------------------------------------------
#
# This section is self-contained: it does not import or redefine ``Provenance``
# (owned by task 2.2). ``is_stale`` accepts any provenance-like object exposing
# the attributes ``pipeline_version``, ``source_fingerprint``, and ``consumed``
# (a list of ``(stage, sha256)`` pairs), inspected structurally via ``getattr``.

# The fixed precedence in which staleness reasons are evaluated. ``is_stale``
# returns at most one reason, choosing the first that applies in this order.
STALE_ABSENT_PROVENANCE = "absent_provenance"
STALE_VERSION = "version"
STALE_SOURCE_FINGERPRINT = "source_fingerprint"
STALE_CONSUMED = "consumed"

# Attributes a provenance-like object must expose to be evaluated for staleness.
_REQUIRED_PROVENANCE_ATTRS = ("pipeline_version", "source_fingerprint", "consumed")

__all__ += [
    "Staleness",
    "is_stale",
    "STALE_ABSENT_PROVENANCE",
    "STALE_VERSION",
    "STALE_SOURCE_FINGERPRINT",
    "STALE_CONSUMED",
]


@dataclass(frozen=True)
class Staleness:
    """One staleness finding: the single reason plus the recorded vs current values.

    ``is_stale`` returns exactly one :class:`Staleness` (or ``None`` when the
    artifact is fresh). The reason strings match the design's precedence list
    (Requirement 3.6): ``absent_provenance``, ``version``, ``source_fingerprint``,
    or ``consumed``.

    Attributes:
        reason: One of the ``STALE_*`` reason strings.
        recorded: The value read from the artifact's provenance (or ``None`` when
            the provenance itself is absent/malformed).
        current: The corresponding current value the artifact is checked against.
        detail: Optional human-readable context, used chiefly for the
            ``consumed`` reason to name the offending stage.
    """

    reason: str
    recorded: Any = None
    current: Any = None
    detail: Optional[str] = None


def _has_provenance_shape(prov: Any) -> bool:
    """Return True when *prov* exposes every required provenance attribute."""
    if prov is None:
        return False
    return all(hasattr(prov, attr) for attr in _REQUIRED_PROVENANCE_ATTRS)


def is_stale(
    prov: Any,
    current_fingerprint: str,
    current_version: str,
    disk_inputs: dict[int, str],
) -> Optional[Staleness]:
    """Classify a cached artifact's provenance as stale, returning one reason or None.

    Reasons are evaluated in a fixed precedence and the first that applies is
    returned (Requirements 3.3, 3.4, 3.5; precedence per design R3.6):

    1. ``absent_provenance`` - *prov* is ``None`` or is missing any of the
       required fields ``pipeline_version``, ``source_fingerprint``,
       ``consumed``.
    2. ``version`` - the recorded ``pipeline_version`` differs from
       *current_version*.
    3. ``source_fingerprint`` - the recorded ``source_fingerprint`` differs from
       *current_fingerprint*.
    4. ``consumed`` - any recorded consumed input's digest differs from the digest
       now on disk in *disk_inputs*, or a recorded consumed input's stage is
       absent from *disk_inputs*.

    When none apply, the artifact is fresh and ``None`` is returned.

    Args:
        prov: A provenance-like object (or ``None``). Inspected structurally, so
            it need not be an instance of any particular class.
        current_fingerprint: The current Source_Fingerprint to compare against.
        current_version: The running Spec_Pipeline version to compare against.
        disk_inputs: Maps a consumed stage number to the SHA-256 digest of that
            input artifact currently on disk.

    Returns:
        A :class:`Staleness` naming the single reason and the recorded vs current
        values, or ``None`` when the artifact is fresh.
    """
    # 1. absent / malformed provenance.
    if not _has_provenance_shape(prov):
        return Staleness(
            reason=STALE_ABSENT_PROVENANCE,
            recorded=None,
            current=current_version,
            detail="provenance is None or missing required fields",
        )

    # 2. pipeline version mismatch.
    recorded_version = getattr(prov, "pipeline_version")
    if recorded_version != current_version:
        return Staleness(
            reason=STALE_VERSION,
            recorded=recorded_version,
            current=current_version,
        )

    # 3. source fingerprint mismatch.
    recorded_fingerprint = getattr(prov, "source_fingerprint")
    if recorded_fingerprint != current_fingerprint:
        return Staleness(
            reason=STALE_SOURCE_FINGERPRINT,
            recorded=recorded_fingerprint,
            current=current_fingerprint,
        )

    # 4. consumed input digest mismatch or missing consumed input.
    consumed = getattr(prov, "consumed") or []
    for entry in consumed:
        stage, recorded_digest = entry[0], entry[1]
        if stage not in disk_inputs:
            return Staleness(
                reason=STALE_CONSUMED,
                recorded=recorded_digest,
                current=None,
                detail=f"consumed input for stage {stage} is absent from disk",
            )
        current_digest = disk_inputs[stage]
        if current_digest != recorded_digest:
            return Staleness(
                reason=STALE_CONSUMED,
                recorded=recorded_digest,
                current=current_digest,
                detail=f"consumed input for stage {stage} changed",
            )

    # Fresh.
    return None
