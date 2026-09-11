"""Ground-truth Pair_Index over ``Paired_Dataset`` (design: R11).

The Evaluation_Harness scores generated CVL specifications against the
human-written specs in ``Paired_Dataset``. Before any scoring can happen it must
discover *which* human spec pairs with *which* Solidity contract, and it must do
so from directory structure and file naming alone - no per-repository code and
no hardcoded repository names (Requirement 11.2).

This module implements that discovery plus the ground-truth property extraction:

* :func:`build_pair_index` - scans the dataset, treats each immediate child
  directory as one repository, and matches one ``.spec`` with one ``.sol`` at
  any depth whose base names (filename without extension) are equal, compared
  case-insensitively. Entries are ordered by repository then contract path
  (Requirements 11.1, 11.2, 11.4).

  Case-insensitivity note: the dataset consistently names a spec as a lower-cased
  spelling of its contract (``pool.spec`` <-> ``Pool.sol``, ``ousd.spec`` <->
  ``OUSD.sol``, ``bentobox.spec`` <-> ``BentoBox.sol``). A strict case-sensitive
  comparison finds only 41 of the 56 pairs, so the "23 repos / 56 pairs from
  structure and naming alone" target of Requirement 11.2 is only reachable with a
  case-insensitive base-name comparison. The recorded ``contract_name`` preserves
  the ``.sol`` file's actual base name so later source name-matching stays exact.
* Unpaired ``.spec`` files (no matching ``.sol``) are recorded with a reason and
  the scan continues (Requirement 11.3). When several ``.sol`` files share the
  base name, the first lexicographic candidate wins and the rest are recorded as
  rejected candidates (Requirement 11.8).
* :func:`write_index` / :func:`read_index` - a round-trippable JSON form: writing
  then reading yields an equal index (Requirement 11.5).
* :func:`extract_ground_truth` - parses a human spec, extracts each ``rule`` /
  ``invariant`` declaration outside comments together with the referenced
  function names and state-variable names that match the paired ``.sol``'s
  declarations, and resolves ``use rule`` / ``use invariant`` against imported
  ``.spec`` files in the same repository (Requirements 11.6, 11.9). Unresolved
  names are recorded and excluded from the count (Requirement 11.10). A spec that
  cannot be parsed is recorded as unscored with the parse error and the scan
  continues (Requirement 11.7).

Import-safety: this module is pure filesystem + regex. It does NOT import
``slither``, ``stage1_extract``, or ``solidity_graph``, so it can be imported and
run on a machine without any Solidity tooling.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "PairEntry",
    "UnpairedSpec",
    "GroundTruthProperty",
    "GroundTruthResult",
    "PairIndex",
    "resolve_dataset_root",
    "build_pair_index",
    "write_index",
    "read_index",
    "extract_ground_truth",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairEntry:
    """One matched ``.sol`` / ``.spec`` pair (Requirement 11.1).

    Attributes:
        repository: The immediate child directory name under the dataset root.
        contract_path: The ``.sol`` path relative to the dataset root (POSIX).
        spec_path: The ``.spec`` path relative to the dataset root (POSIX).
        contract_name: The shared base name (filename without extension).
        rejected_contracts: Other ``.sol`` candidates that shared the base name
            but lost the first-lexicographic tie-break (Requirement 11.8).
    """

    repository: str
    contract_path: str
    spec_path: str
    contract_name: str
    rejected_contracts: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnpairedSpec:
    """A ``.spec`` file with no matching ``.sol`` (Requirement 11.3).

    Attributes:
        repository: The repository directory the spec lives under.
        spec_path: The ``.spec`` path relative to the dataset root (POSIX).
        contract_name: The base name that found no ``.sol`` match.
        reason: A human-readable reason for the mismatch.
    """

    repository: str
    spec_path: str
    contract_name: str
    reason: str


@dataclass(frozen=True)
class GroundTruthProperty:
    """One rule or invariant declared in a human spec (Requirement 11.6).

    Attributes:
        name: The declared rule/invariant name.
        kind: Either ``"rule"`` or ``"invariant"``.
        functions: Referenced function names that match a ``.sol`` declaration.
        state_variables: Referenced state-variable names that match a ``.sol``
            declaration.
        origin: The base name of the ``.spec`` file the declaration came from -
            equal to the analyzed spec for own declarations, or the imported
            spec for a resolved ``use`` reference (Requirement 11.9).
    """

    name: str
    kind: str
    functions: tuple[str, ...] = ()
    state_variables: tuple[str, ...] = ()
    origin: str = ""


@dataclass
class GroundTruthResult:
    """The parsed ground-truth of one spec (Requirements 11.6, 11.7, 11.10).

    Attributes:
        properties: Extracted properties, counted toward the ground-truth total.
        count: Number of counted properties (own declarations plus resolved
            ``use`` references). A spec with no own rule/invariant counts 0.
        unresolved_uses: ``use rule`` / ``use invariant`` names that could not be
            resolved against an imported spec; excluded from ``count``
            (Requirement 11.10).
        parse_error: Set when the spec could not be parsed; the pair is then
            unscored but retained (Requirement 11.7).
    """

    properties: list[GroundTruthProperty] = field(default_factory=list)
    count: int = 0
    unresolved_uses: list[str] = field(default_factory=list)
    parse_error: Optional[str] = None


@dataclass
class PairIndex:
    """The ordered, round-trippable Pair_Index (Requirements 11.1, 11.4, 11.5)."""

    entries: list[PairEntry] = field(default_factory=list)
    unpaired: list[UnpairedSpec] = field(default_factory=list)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PairIndex):
            return NotImplemented
        return self.entries == other.entries and self.unpaired == other.unpaired


# ---------------------------------------------------------------------------
# Dataset root resolution
# ---------------------------------------------------------------------------


def resolve_dataset_root(base: Optional[Path] = None) -> Path:
    """Resolve the ``Paired_Dataset`` directory.

    Priority order mirrors the rest of the codebase (scraper.py):

    1. an explicit *base* argument (may point at the repo root or the
       ``Paired_Dataset`` directory itself),
    2. the ``CERTORA_DATASET_ROOT`` environment variable,
    3. the repository root inferred from this module's location.

    Args:
        base: Optional explicit base directory.

    Returns:
        The resolved ``Paired_Dataset`` directory.
    """
    candidates: list[Optional[Path]] = [
        Path(base) if base is not None else None,
        Path(os.environ["CERTORA_DATASET_ROOT"])
        if os.environ.get("CERTORA_DATASET_ROOT")
        else None,
        # spec_pipeline/eval/pair_index.py -> repo root is three parents up.
        Path(__file__).resolve().parents[2],
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = candidate.expanduser()
        # Accept either the repo root (holding Paired_Dataset) or the dataset
        # directory itself.
        if candidate.name == "Paired_Dataset":
            return candidate
        nested = candidate / "Paired_Dataset"
        if nested.exists():
            return nested
        # Fall through to the next candidate when this one does not resolve.
    # Last resort: repo-root/Paired_Dataset even if absent, so callers get a
    # predictable path they can test against.
    return Path(__file__).resolve().parents[2] / "Paired_Dataset"


# ---------------------------------------------------------------------------
# Pair index construction
# ---------------------------------------------------------------------------


def _iter_files(root: Path, suffix: str) -> list[Path]:
    """Return every file under *root* with *suffix*, sorted by POSIX relpath."""
    return sorted(
        (p for p in root.rglob(f"*{suffix}") if p.is_file()),
        key=lambda p: p.as_posix(),
    )


def build_pair_index(base: Optional[Path] = None) -> PairIndex:
    """Scan the dataset and build the Pair_Index (Requirements 11.1-11.4, 11.8).

    Each immediate child directory of the dataset root is one repository. Within
    a repository, a ``.spec`` matches a ``.sol`` at any depth whose base name
    (filename without extension) is equal, compared case-insensitively (see the
    module docstring for why: the required 56-pair total is only reachable that
    way). When more than one ``.sol`` shares the base name, the first
    lexicographic candidate is chosen and the remaining candidates are recorded
    as ``rejected_contracts`` (Requirement 11.8). A ``.spec`` with no matching
    ``.sol`` is recorded as unpaired with a reason (Requirement 11.3).

    Entries are ordered by repository name then contract path (Requirement
    11.4).

    Args:
        base: Optional dataset base; see :func:`resolve_dataset_root`.

    Returns:
        The populated :class:`PairIndex`.
    """
    dataset_root = resolve_dataset_root(base)
    index = PairIndex()

    if not dataset_root.exists():
        return index

    repos = sorted(
        (d for d in dataset_root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )

    entries: list[PairEntry] = []
    unpaired: list[UnpairedSpec] = []

    for repo_dir in repos:
        repo_name = repo_dir.name

        # Group every .sol in this repo by its case-folded base name.
        sol_by_base: dict[str, list[Path]] = {}
        for sol_path in _iter_files(repo_dir, ".sol"):
            sol_by_base.setdefault(sol_path.stem.lower(), []).append(sol_path)

        for spec_path in _iter_files(repo_dir, ".spec"):
            base_name = spec_path.stem
            spec_rel = spec_path.relative_to(dataset_root).as_posix()
            candidates = sol_by_base.get(base_name.lower(), [])

            if not candidates:
                unpaired.append(
                    UnpairedSpec(
                        repository=repo_name,
                        spec_path=spec_rel,
                        contract_name=base_name,
                        reason=(
                            f"no .sol with base name '{base_name}' found in "
                            f"repository '{repo_name}'"
                        ),
                    )
                )
                continue

            # First lexicographic candidate wins; record the rest as rejected.
            ordered = sorted(candidates, key=lambda p: p.as_posix())
            chosen = ordered[0]
            rejected = tuple(
                p.relative_to(dataset_root).as_posix() for p in ordered[1:]
            )
            entries.append(
                PairEntry(
                    repository=repo_name,
                    contract_path=chosen.relative_to(dataset_root).as_posix(),
                    spec_path=spec_rel,
                    # Preserve the actual .sol base name (its true casing) so
                    # source name-matching stays exact.
                    contract_name=chosen.stem,
                    rejected_contracts=rejected,
                )
            )

    entries.sort(key=lambda e: (e.repository, e.contract_path))
    unpaired.sort(key=lambda u: (u.repository, u.spec_path))
    index.entries = entries
    index.unpaired = unpaired
    return index


# ---------------------------------------------------------------------------
# Round-trippable persistence (Requirement 11.5)
# ---------------------------------------------------------------------------


def _entry_to_dict(entry: PairEntry) -> dict:
    return {
        "repository": entry.repository,
        "contract_path": entry.contract_path,
        "spec_path": entry.spec_path,
        "contract_name": entry.contract_name,
        "rejected_contracts": list(entry.rejected_contracts),
    }


def _entry_from_dict(payload: dict) -> PairEntry:
    return PairEntry(
        repository=payload["repository"],
        contract_path=payload["contract_path"],
        spec_path=payload["spec_path"],
        contract_name=payload["contract_name"],
        rejected_contracts=tuple(payload.get("rejected_contracts", [])),
    )


def _unpaired_to_dict(unpaired: UnpairedSpec) -> dict:
    return {
        "repository": unpaired.repository,
        "spec_path": unpaired.spec_path,
        "contract_name": unpaired.contract_name,
        "reason": unpaired.reason,
    }


def _unpaired_from_dict(payload: dict) -> UnpairedSpec:
    return UnpairedSpec(
        repository=payload["repository"],
        spec_path=payload["spec_path"],
        contract_name=payload["contract_name"],
        reason=payload["reason"],
    )


def index_to_dict(index: PairIndex) -> dict:
    """Return the JSON-serializable form of *index*."""
    return {
        "entries": [_entry_to_dict(e) for e in index.entries],
        "unpaired": [_unpaired_to_dict(u) for u in index.unpaired],
    }


def index_from_dict(payload: dict) -> PairIndex:
    """Rebuild a :class:`PairIndex` from its JSON form."""
    return PairIndex(
        entries=[_entry_from_dict(e) for e in payload.get("entries", [])],
        unpaired=[_unpaired_from_dict(u) for u in payload.get("unpaired", [])],
    )


def write_index(index: PairIndex, path: Path) -> Path:
    """Write *index* to *path* as canonical JSON (Requirement 11.5).

    The JSON is written with sorted keys, two-space indentation, and a trailing
    newline, matching the Artifact_Store convention (Requirement 21.7), so
    writing then reading yields an equal index.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(index_to_dict(index), sort_keys=True, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read_index(path: Path) -> PairIndex:
    """Read a :class:`PairIndex` previously written by :func:`write_index`."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return index_from_dict(payload)


# ---------------------------------------------------------------------------
# Comment stripping (shared by spec + sol parsing)
# ---------------------------------------------------------------------------

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(text: str) -> str:
    """Remove ``//`` line comments and ``/* */`` block comments.

    Comment spans are replaced by whitespace of the same newline count so line
    numbers and token boundaries are preserved for downstream regex matching.
    """

    def _block_repl(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    without_block = _BLOCK_COMMENT.sub(_block_repl, text)
    without_line = _LINE_COMMENT.sub("", without_block)
    return without_line


# ---------------------------------------------------------------------------
# Solidity declaration extraction (regex, no slither) - Requirement 11.6
# ---------------------------------------------------------------------------

_SOL_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
# State variables: `<type> [visibility/modifiers...] name [= ...];` at a
# statement position. We keep this conservative: capture the last identifier
# before `;` or `=` on lines that declare a typed member. To avoid false
# positives we require the line to not contain `(` (which would indicate a
# function/modifier) before the name.
_SOL_STATE_VAR = re.compile(
    r"(?m)^\s*"
    r"(?:mapping\s*\([^;{]*\)|[A-Za-z_$][\w$.\[\]]*)"  # type (mapping or named)
    r"(?:\s+(?:public|private|internal|external|constant|immutable|override|payable|memory|storage|calldata))*"
    r"\s+([A-Za-z_$][\w$]*)\s*(?:=[^;]*)?;"
)


def extract_sol_declarations(sol_text: str) -> tuple[set[str], set[str]]:
    """Extract declared function and state-variable names from Solidity source.

    Uses regex over comment-stripped text (no slither), returning
    ``(function_names, state_variable_names)``. The extraction is intentionally
    permissive: its purpose is to give the ground-truth matcher a set of names to
    intersect the spec's referenced identifiers against (Requirement 11.6), not
    to be a full parser.
    """
    body = _strip_comments(sol_text)
    functions = set(_SOL_FUNCTION.findall(body))
    state_vars = set(_SOL_STATE_VAR.findall(body))
    # A name can be captured as both by the permissive patterns; functions win.
    state_vars -= functions
    return functions, state_vars


# ---------------------------------------------------------------------------
# CVL spec parsing (regex, comments removed) - Requirements 11.6, 11.9, 11.10
# ---------------------------------------------------------------------------

_IMPORT_RE = re.compile(r'(?m)^\s*import\s+"([^"]+)"\s*;?')
_RULE_RE = re.compile(r"\brule\s+([A-Za-z_$][\w$]*)\b")
_INVARIANT_RE = re.compile(r"\binvariant\s+([A-Za-z_$][\w$]*)\b")
_USE_RULE_RE = re.compile(r"\buse\s+rule\s+([A-Za-z_$][\w$]*)\b")
_USE_INVARIANT_RE = re.compile(r"\buse\s+invariant\s+([A-Za-z_$][\w$]*)\b")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _find_declaration_body(text: str, keyword: str, name: str, start: int) -> str:
    """Return the text of a rule/invariant declaration body from *start*.

    For a ``rule`` the body is the brace-delimited block ``{ ... }``. For an
    ``invariant`` the body runs from the declaration to the next top-level
    declaration or end of file (invariants may have no braces). This is used to
    scope which identifiers count as "referenced by" the declaration.
    """
    # Find the first `{` after start; balance braces to find the block.
    brace_open = text.find("{", start)
    if brace_open != -1:
        depth = 0
        i = brace_open
        while i < len(text):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1
        return text[start:]
    # No brace: invariant expression up to the next declaration keyword.
    tail = text[start:]
    next_decl = re.search(
        r"\b(rule|invariant|methods|ghost|hook|definition|function|use)\b",
        tail[len(keyword):],
    )
    if next_decl:
        return tail[: len(keyword) + next_decl.start()]
    return tail


def _iter_declarations(body: str):
    """Yield ``(kind, name, match_start)`` for each rule/invariant declaration."""
    for match in _RULE_RE.finditer(body):
        yield "rule", match.group(1), match.start()
    for match in _INVARIANT_RE.finditer(body):
        yield "invariant", match.group(1), match.start()


def _references(decl_body: str, sol_functions: set[str], sol_state_vars: set[str]):
    """Return referenced function/state-var names present in the .sol."""
    idents = set(_IDENT_RE.findall(decl_body))
    functions = tuple(sorted(idents & sol_functions))
    state_vars = tuple(sorted(idents & sol_state_vars))
    return functions, state_vars


def _parse_spec_declarations(
    spec_text: str,
    origin: str,
    sol_functions: set[str],
    sol_state_vars: set[str],
) -> tuple[dict[str, GroundTruthProperty], dict[str, GroundTruthProperty]]:
    """Parse own rule/invariant declarations from one spec text.

    Returns two name->property maps: ``rules`` and ``invariants`` (kept separate
    so ``use rule`` resolves only against rules and ``use invariant`` only
    against invariants).
    """
    body = _strip_comments(spec_text)
    rules: dict[str, GroundTruthProperty] = {}
    invariants: dict[str, GroundTruthProperty] = {}

    for kind, name, start in _iter_declarations(body):
        # Skip `use rule`/`use invariant` matches: those are handled separately.
        prefix = body[max(0, start - 4): start]
        if prefix.strip().endswith("use"):
            continue
        decl_body = _find_declaration_body(body, kind, name, start)
        functions, state_vars = _references(decl_body, sol_functions, sol_state_vars)
        prop = GroundTruthProperty(
            name=name,
            kind=kind,
            functions=functions,
            state_variables=state_vars,
            origin=origin,
        )
        if kind == "rule":
            rules[name] = prop
        else:
            invariants[name] = prop
    return rules, invariants


def extract_ground_truth(
    spec_path: Path,
    sol_path: Path,
    repo_dir: Optional[Path] = None,
) -> GroundTruthResult:
    """Extract Ground_Truth_Properties from a human spec (Requirements 11.6-11.10).

    The spec is parsed for its own ``rule`` and ``invariant`` declarations
    (outside comments), and for each one the referenced function names and
    state-variable names that also appear in the paired ``.sol``'s declarations
    are recorded (Requirement 11.6). ``use rule`` / ``use invariant`` references
    are resolved against imported ``.spec`` files in the same repository
    (Requirement 11.9); names that resolve are counted, names that do not are
    recorded in ``unresolved_uses`` and excluded from the count (Requirement
    11.10). A spec with no own rule/invariant and no resolved use counts 0
    (Requirement 11.6). If the spec cannot be read/parsed, the result carries a
    ``parse_error`` and no properties (Requirement 11.7).

    Args:
        spec_path: Path to the human-written ``.spec`` file.
        sol_path: Path to the paired ``.sol`` file (for name matching).
        repo_dir: Repository directory used to resolve ``import`` targets and
            ``use`` references. Defaults to the spec's parent directory.

    Returns:
        The :class:`GroundTruthResult`.
    """
    spec_path = Path(spec_path)
    result = GroundTruthResult()

    try:
        spec_text = spec_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        result.parse_error = f"could not read spec: {exc}"
        return result

    try:
        sol_functions: set[str] = set()
        sol_state_vars: set[str] = set()
        try:
            sol_text = Path(sol_path).read_text(encoding="utf-8", errors="ignore")
            sol_functions, sol_state_vars = extract_sol_declarations(sol_text)
        except OSError:
            # Missing/unreadable .sol -> no name matches, but not a spec parse
            # error; ground-truth still counts the declarations themselves.
            pass

        origin = spec_path.stem
        own_rules, own_invariants = _parse_spec_declarations(
            spec_text, origin, sol_functions, sol_state_vars
        )

        # Resolve use rule / use invariant against imported specs in the repo.
        if repo_dir is None:
            repo_dir = spec_path.parent
        repo_dir = Path(repo_dir)

        clean_spec = _strip_comments(spec_text)
        use_rules = _USE_RULE_RE.findall(clean_spec)
        use_invariants = _USE_INVARIANT_RE.findall(clean_spec)

        resolved_extra: dict[str, GroundTruthProperty] = {}
        if use_rules or use_invariants:
            imported_rules, imported_invariants = _load_imported_declarations(
                spec_text, spec_path, repo_dir, sol_functions, sol_state_vars
            )
            for name in use_rules:
                prop = imported_rules.get(name) or own_rules.get(name)
                if prop is not None:
                    resolved_extra[f"rule:{name}"] = prop
                else:
                    result.unresolved_uses.append(f"rule {name}")
            for name in use_invariants:
                prop = imported_invariants.get(name) or own_invariants.get(name)
                if prop is not None:
                    resolved_extra[f"invariant:{name}"] = prop
                else:
                    result.unresolved_uses.append(f"invariant {name}")

        properties: list[GroundTruthProperty] = []
        properties.extend(own_rules.values())
        properties.extend(own_invariants.values())
        # Add resolved use-references that are not already own declarations.
        own_keys = {f"rule:{n}" for n in own_rules} | {
            f"invariant:{n}" for n in own_invariants
        }
        for key, prop in resolved_extra.items():
            if key not in own_keys:
                properties.append(prop)

        result.properties = properties
        result.count = len(properties)
        return result
    except Exception as exc:  # noqa: BLE001 - classify any parse fault (R11.7)
        result.parse_error = f"parse error: {type(exc).__name__}: {exc}"
        result.properties = []
        result.count = 0
        return result


def _load_imported_declarations(
    spec_text: str,
    spec_path: Path,
    repo_dir: Path,
    sol_functions: set[str],
    sol_state_vars: set[str],
    _seen: Optional[set[Path]] = None,
) -> tuple[dict[str, GroundTruthProperty], dict[str, GroundTruthProperty]]:
    """Recursively load rule/invariant declarations from imported specs.

    ``import "X.spec";`` statements are resolved to ``.spec`` files located in
    the same repository directory (Requirement 11.9). Missing imports are simply
    skipped - their ``use`` references then stay unresolved (Requirement 11.10).
    """
    if _seen is None:
        _seen = set()

    rules: dict[str, GroundTruthProperty] = {}
    invariants: dict[str, GroundTruthProperty] = {}

    clean = _strip_comments(spec_text)
    for target in _IMPORT_RE.findall(clean):
        target_name = Path(target).name  # allow "dir/X.spec"
        resolved = _resolve_import(target_name, spec_path, repo_dir)
        if resolved is None or resolved in _seen:
            continue
        _seen.add(resolved)
        try:
            imported_text = resolved.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        i_rules, i_invs = _parse_spec_declarations(
            imported_text, resolved.stem, sol_functions, sol_state_vars
        )
        rules.update(i_rules)
        invariants.update(i_invs)
        # Recurse into transitive imports within the same repo.
        t_rules, t_invs = _load_imported_declarations(
            imported_text, resolved, repo_dir, sol_functions, sol_state_vars, _seen
        )
        for k, v in t_rules.items():
            rules.setdefault(k, v)
        for k, v in t_invs.items():
            invariants.setdefault(k, v)

    return rules, invariants


def _resolve_import(
    target_name: str, spec_path: Path, repo_dir: Path
) -> Optional[Path]:
    """Resolve an imported ``.spec`` name to a file within the repository.

    Search order: the importing spec's own directory first, then anywhere under
    the repository directory (first lexicographic match wins).
    """
    sibling = spec_path.parent / target_name
    if sibling.is_file():
        return sibling
    matches = sorted(
        (p for p in repo_dir.rglob(target_name) if p.is_file()),
        key=lambda p: p.as_posix(),
    )
    return matches[0] if matches else None
