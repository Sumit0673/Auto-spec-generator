"""
Project_Resolver + Dependency_Resolver (design: R7, R8, R19).

This module answers two questions that the pipeline previously answered with a
single hardcoded path (``analyzer._SHARED_DEPS``):

1. *Where are the shared Solidity dependencies?* — :class:`Dependency_Resolver`
   collects candidate dependency roots from ``--deps-root`` arguments, the
   ``SOLIDITY_DEPS_ROOT`` environment variable, and the ``node_modules`` / ``lib``
   directories discovered by walking from the analyzed path up to the filesystem
   root, nearest-ancestor first, deduping by resolved absolute path
   (Requirements 7.2, 7.3, 7.8).

2. *What project layout is this, and how do we compile it?* —
   :class:`Project_Resolver` picks the project root by the first upward build
   marker, computes import remappings for Foundry / Hardhat / npm / bare layouts,
   and selects the highest installed solc version compatible with the first-party
   pragmas (Requirements 8.2-8.7, 8.12).

The module is deliberately pure and toolchain-free: it never imports slither,
never shells out to solc or certoraRun, and takes the installed-solc list and the
analyzed ``.sol`` paths as arguments. That keeps it fully unit-testable against
temp-directory fixtures. Wiring these resolvers into ``analyzer.py`` (task 6.4)
and ``stage5_verify.py`` (task 6.6) is done elsewhere.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "DependencyRoot",
    "DependencyResolution",
    "Dependency_Resolver",
    "Remapping",
    "SolcSelection",
    "ProjectResolution",
    "Project_Resolver",
    "PragmaConstraint",
    "parse_pragma",
    "version_satisfies",
    "select_solc",
]


# ---------------------------------------------------------------------------
# Dependency_Resolver
# ---------------------------------------------------------------------------


@dataclass
class DependencyRoot:
    """One resolved (or rejected) dependency root, for the Run_Manifest."""

    path: str
    source: str  # "deps-root-arg" | "env" | "walk"
    ok: bool
    reason: Optional[str] = None  # populated when ok is False


@dataclass
class DependencyResolution:
    """Result of :meth:`Dependency_Resolver.resolve`.

    ``roots`` is the deduped, ordered list of usable dependency-root directories
    (as resolved absolute :class:`~pathlib.Path`). ``records`` holds one entry per
    candidate considered — both accepted and rejected — for the Run_Manifest
    (Requirement 7.3).
    """

    roots: list[Path] = field(default_factory=list)
    records: list[DependencyRoot] = field(default_factory=list)


class Dependency_Resolver:
    """Locate shared Solidity dependency roots (``node_modules`` / ``lib``).

    Candidate ordering (Requirement 7.2):

    1. explicit ``--deps-root`` arguments, in the order supplied;
    2. ``SOLIDITY_DEPS_ROOT`` entries, split on :data:`os.pathsep`, in order;
    3. every ``node_modules`` and ``lib`` directory found by walking from the
       analyzed path up to the filesystem root, nearest-ancestor first.

    Absent / non-directory / unreadable candidates are recorded with a reason and
    skipped; resolution continues with the rest, yielding an empty root set when
    none resolve (Requirement 7.3). The final root list is deduped by resolved
    absolute path, keeping the first occurrence (Requirement 7.8).
    """

    def __init__(
        self,
        deps_root_args: Optional[Iterable[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._deps_root_args = list(deps_root_args or [])
        self._env = env if env is not None else dict(os.environ)

    def resolve(self, analyzed_path: Path) -> DependencyResolution:
        analyzed_path = Path(analyzed_path)
        result = DependencyResolution()
        seen: set[Path] = set()

        def consider(raw: str, source: str) -> None:
            candidate = Path(raw).expanduser()
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
                result.records.append(
                    DependencyRoot(str(candidate), source, False, f"unresolvable: {exc}")
                )
                return
            reason = self._reject_reason(resolved)
            if reason is not None:
                result.records.append(DependencyRoot(str(resolved), source, False, reason))
                return
            if resolved in seen:
                result.records.append(
                    DependencyRoot(str(resolved), source, False, "duplicate")
                )
                return
            seen.add(resolved)
            result.roots.append(resolved)
            result.records.append(DependencyRoot(str(resolved), source, True))

        for raw in self._deps_root_args:
            consider(raw, "deps-root-arg")

        env_value = self._env.get("SOLIDITY_DEPS_ROOT", "")
        for raw in env_value.split(os.pathsep):
            if raw:
                consider(raw, "env")

        for walked in self._walk_candidates(analyzed_path):
            consider(str(walked), "walk")

        return result

    @staticmethod
    def _reject_reason(path: Path) -> Optional[str]:
        if not path.exists():
            return "absent"
        if not path.is_dir():
            return "not-a-directory"
        if not os.access(path, os.R_OK | os.X_OK):
            return "unreadable"
        return None

    @staticmethod
    def _walk_candidates(analyzed_path: Path) -> list[Path]:
        """Yield ``node_modules`` and ``lib`` dirs from nearest ancestor upward."""
        start = analyzed_path if analyzed_path.is_dir() else analyzed_path.parent
        try:
            start = start.resolve()
        except (OSError, RuntimeError):  # pragma: no cover - defensive
            return []
        out: list[Path] = []
        for directory in [start, *start.parents]:
            for name in ("node_modules", "lib"):
                candidate = directory / name
                if candidate.is_dir():
                    out.append(candidate)
        return out


# ---------------------------------------------------------------------------
# Pragma parsing + semver range check (focused, solidity-only)
# ---------------------------------------------------------------------------


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# One comparator token inside a pragma, e.g. "^0.8.0", ">=0.7.0", "<0.9.0", "=0.8.19".
_COMPARATOR_RE = re.compile(r"(\^|~|>=|<=|>|<|=)?\s*(\d+)\.(\d+)(?:\.(\d+))?")


def _parse_version(text: str) -> tuple[int, int, int]:
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise ValueError(f"not a full semver version: {text!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@dataclass(frozen=True)
class PragmaConstraint:
    """One comparator drawn from a ``pragma solidity`` line.

    ``op`` is one of ``^ ~ >= <= > < =``. ``version`` is a (major, minor, patch)
    tuple; a missing patch is normalized to 0.
    """

    op: str
    version: tuple[int, int, int]

    def allows(self, ver: tuple[int, int, int]) -> bool:
        op, base = self.op, self.version
        if op == "=":
            return ver == base
        if op == ">=":
            return ver >= base
        if op == "<=":
            return ver <= base
        if op == ">":
            return ver > base
        if op == "<":
            return ver < base
        if op == "^":
            # Caret: >= base and < next-nonzero-left-most bump.
            if ver < base:
                return False
            if base[0] > 0:
                return ver[0] == base[0]
            # 0.x.y -> compatible within the same minor (solidity semantics)
            if base[1] > 0:
                return ver[0] == 0 and ver[1] == base[1]
            return ver[0] == 0 and ver[1] == 0 and ver[2] == base[2]
        if op == "~":
            # Tilde: >= base and < next minor.
            if ver < base:
                return False
            return ver[0] == base[0] and ver[1] == base[1]
        raise ValueError(f"unknown operator: {op!r}")


def parse_pragma(pragma_text: str) -> list[PragmaConstraint]:
    """Parse a ``pragma solidity ...`` expression into comparator constraints.

    Accepts either a full pragma line or just the version expression. A bare
    version such as ``0.8.19`` is treated as ``=0.8.19``. Compound expressions
    like ``>=0.7.0 <0.9.0`` yield one constraint per comparator.
    """
    text = pragma_text.strip()
    text = re.sub(r"^pragma\s+solidity", "", text, flags=re.IGNORECASE)
    text = text.rstrip(";").strip()
    constraints: list[PragmaConstraint] = []
    for m in _COMPARATOR_RE.finditer(text):
        op = m.group(1) or "="
        major = int(m.group(2))
        minor = int(m.group(3))
        patch = int(m.group(4)) if m.group(4) is not None else 0
        constraints.append(PragmaConstraint(op, (major, minor, patch)))
    return constraints


def version_satisfies(
    version: str | tuple[int, int, int], constraints: Iterable[PragmaConstraint]
) -> bool:
    """Return True when ``version`` satisfies every constraint."""
    ver = version if isinstance(version, tuple) else _parse_version(version)
    return all(c.allows(ver) for c in constraints)


# ---------------------------------------------------------------------------
# solc selection
# ---------------------------------------------------------------------------


@dataclass
class SolcSelection:
    """Outcome of :func:`select_solc`.

    Exactly one of ``version`` / ``outcome`` describes the result:

    * a compatible installed version was chosen → ``version`` and ``path`` set,
      ``outcome`` is ``None``;
    * a version could satisfy every constraint but none is installed →
      ``outcome == "no_compatible_solc"``;
    * first-party files declare disjoint constraints with no common version →
      ``outcome == "unsupported_pragma_set"`` and ``conflicting_files`` names them.
    """

    version: Optional[str] = None
    path: Optional[str] = None
    outcome: Optional[str] = None
    constraints: list[PragmaConstraint] = field(default_factory=list)
    installed_versions: list[str] = field(default_factory=list)
    conflicting_files: list[str] = field(default_factory=list)


# A conservative upper bound used only to decide whether *some* version could
# satisfy the constraints (the no_compatible_solc vs unsupported_pragma_set
# distinction). It is not tied to any installed toolchain.
_SATISFIABILITY_PROBE = [
    (0, minor, patch) for minor in range(0, 40) for patch in range(0, 40)
]


def _constraints_are_satisfiable(constraints: list[PragmaConstraint]) -> bool:
    if not constraints:
        return True
    return any(
        all(c.allows(ver) for c in constraints) for ver in _SATISFIABILITY_PROBE
    )


def select_solc(
    installed: Iterable[tuple[str, str]],
    per_file_constraints: dict[str, list[PragmaConstraint]],
) -> SolcSelection:
    """Choose the highest installed solc satisfying every first-party pragma.

    Parameters
    ----------
    installed:
        Iterable of ``(version, path)`` pairs for the installed solc binaries.
    per_file_constraints:
        Mapping of first-party file path → the constraints that file declares.

    Returns a :class:`SolcSelection`. When first-party files split into two or
    more groups with no version common to all, the outcome is
    ``unsupported_pragma_set`` (Requirement 8.7). When the combined constraints
    are satisfiable but no installed version satisfies them, the outcome is
    ``no_compatible_solc`` (Requirement 8.6).
    """
    all_constraints: list[PragmaConstraint] = []
    for constraints in per_file_constraints.values():
        all_constraints.extend(constraints)

    installed_list = list(installed)
    installed_versions = [v for v, _ in installed_list]

    # Disjoint first-party constraints: no version can satisfy every file.
    if not _constraints_are_satisfiable(all_constraints):
        conflicting = _find_conflicting_files(per_file_constraints)
        return SolcSelection(
            outcome="unsupported_pragma_set",
            constraints=all_constraints,
            installed_versions=installed_versions,
            conflicting_files=conflicting,
        )

    # Highest installed version satisfying every constraint.
    compatible: list[tuple[tuple[int, int, int], str, str]] = []
    for version, path in installed_list:
        try:
            parsed = _parse_version(version)
        except ValueError:
            continue
        if version_satisfies(parsed, all_constraints):
            compatible.append((parsed, version, path))

    if not compatible:
        return SolcSelection(
            outcome="no_compatible_solc",
            constraints=all_constraints,
            installed_versions=installed_versions,
        )

    compatible.sort(key=lambda t: t[0])
    _, best_version, best_path = compatible[-1]
    return SolcSelection(
        version=best_version,
        path=best_path,
        constraints=all_constraints,
        installed_versions=installed_versions,
    )


def _find_conflicting_files(
    per_file_constraints: dict[str, list[PragmaConstraint]]
) -> list[str]:
    """Return the smallest set of files whose combined constraints are unsatisfiable.

    Greedily grow a group of files until it becomes unsatisfiable; the file that
    tips it over plus the group so far are the conflicting files. Returns all
    files with declared constraints when a minimal pair cannot be isolated.
    """
    files_with_constraints = [
        (f, c) for f, c in sorted(per_file_constraints.items()) if c
    ]
    accumulated: list[PragmaConstraint] = []
    group: list[str] = []
    for path, constraints in files_with_constraints:
        candidate = accumulated + constraints
        if not _constraints_are_satisfiable(candidate):
            group.append(path)
            return group
        accumulated = candidate
        group.append(path)
    return [f for f, _ in files_with_constraints]


# ---------------------------------------------------------------------------
# Project_Resolver
# ---------------------------------------------------------------------------


_BUILD_MARKERS = (
    "foundry.toml",
    "hardhat.config.js",
    "hardhat.config.ts",
    "package.json",
)


@dataclass
class Remapping:
    """A single ``prefix=target`` import remapping."""

    prefix: str
    target: str

    def as_arg(self) -> str:
        return f"{self.prefix}={self.target}"


@dataclass
class ProjectResolution:
    """Everything the Verifier needs to invoke certoraRun for a project."""

    project_root: Path
    layout: str  # "foundry" | "hardhat" | "npm" | "bare"
    marker: Optional[str]  # the file that selected the root, if any
    remappings: list[Remapping] = field(default_factory=list)
    solc: Optional[SolcSelection] = None

    def remapping_args(self) -> list[str]:
        return [r.as_arg() for r in self.remappings]


class Project_Resolver:
    """Determine project root, import remappings, and solc selection.

    The root is the first directory — walking from the analyzed sources up to the
    filesystem root — that holds one of :data:`_BUILD_MARKERS`, recording which
    marker selected it. When no marker is found, the root is the deepest directory
    containing all analyzed ``.sol`` files (the parent directory for a single
    file) (Requirements 8.4, 8.12).
    """

    def __init__(self, dependency_resolution: Optional[DependencyResolution] = None) -> None:
        self._deps = dependency_resolution or DependencyResolution()

    def resolve(
        self,
        analyzed_sources: Iterable[Path],
        installed_solc: Optional[Iterable[tuple[str, str]]] = None,
    ) -> ProjectResolution:
        sources = [Path(s) for s in analyzed_sources]
        if not sources:
            raise ValueError("analyzed_sources must contain at least one path")

        root, marker = self._find_root(sources)
        layout = self._layout_for_marker(marker)

        if layout == "foundry":
            remappings = self._foundry_remappings(root)
        elif layout in ("hardhat", "npm"):
            remappings = self._node_modules_remappings(root)
        else:
            remappings = self._dependency_root_remappings()

        solc = None
        if installed_solc is not None:
            per_file = self._first_party_constraints(sources)
            solc = select_solc(installed_solc, per_file)

        return ProjectResolution(
            project_root=root,
            layout=layout,
            marker=marker,
            remappings=remappings,
            solc=solc,
        )

    # -- root selection -----------------------------------------------------

    def _find_root(self, sources: list[Path]) -> tuple[Path, Optional[str]]:
        start = self._deepest_common_dir(sources)
        for directory in [start, *start.parents]:
            for marker in _BUILD_MARKERS:
                if (directory / marker).is_file():
                    return directory.resolve(), marker
        return start.resolve(), None

    @staticmethod
    def _deepest_common_dir(sources: list[Path]) -> Path:
        dirs = []
        for src in sources:
            resolved = src.resolve()
            dirs.append(resolved if resolved.is_dir() else resolved.parent)
        if len(dirs) == 1:
            return dirs[0]
        common = Path(os.path.commonpath([str(d) for d in dirs]))
        return common

    @staticmethod
    def _layout_for_marker(marker: Optional[str]) -> str:
        if marker == "foundry.toml":
            return "foundry"
        if marker in ("hardhat.config.js", "hardhat.config.ts"):
            return "hardhat"
        if marker == "package.json":
            return "npm"
        return "bare"

    # -- remappings ---------------------------------------------------------

    def _foundry_remappings(self, root: Path) -> list[Remapping]:
        remappings: dict[str, str] = {}

        remap_file = root / "remappings.txt"
        if remap_file.is_file():
            for line in remap_file.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                prefix, target = line.split("=", 1)
                remappings[prefix.strip()] = target.strip()

        foundry_toml = root / "foundry.toml"
        if foundry_toml.is_file():
            for prefix, target in self._parse_toml_remappings(foundry_toml):
                remappings.setdefault(prefix, target)

        # lib/* directories become <name>/=lib/<name>/
        lib_dir = root / "lib"
        if lib_dir.is_dir():
            for entry in sorted(lib_dir.iterdir()):
                if entry.is_dir():
                    prefix = f"{entry.name}/"
                    remappings.setdefault(prefix, f"{entry}/")

        return [Remapping(p, t) for p, t in sorted(remappings.items())]

    @staticmethod
    def _parse_toml_remappings(foundry_toml: Path) -> list[tuple[str, str]]:
        """Extract ``remappings = [ "a=b", ... ]`` from foundry.toml.

        A focused parser: it finds the ``remappings`` array (single- or
        multi-line) and pulls each quoted ``prefix=target`` entry. This avoids a
        hard dependency on a TOML library for the one field we need.
        """
        text = foundry_toml.read_text(errors="replace")
        m = re.search(r"remappings\s*=\s*\[(.*?)\]", text, flags=re.DOTALL)
        if not m:
            return []
        body = m.group(1)
        out: list[tuple[str, str]] = []
        for entry in re.findall(r"""['"]([^'"]+)['"]""", body):
            if "=" in entry:
                prefix, target = entry.split("=", 1)
                out.append((prefix.strip(), target.strip()))
        return out

    def _node_modules_remappings(self, root: Path) -> list[Remapping]:
        """Map ``node_modules`` packages that contain a ``.sol`` at any depth."""
        node_modules = root / "node_modules"
        remappings: dict[str, str] = {}
        if not node_modules.is_dir():
            return []
        for entry in sorted(node_modules.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("@"):
                # Scoped packages: node_modules/@scope/pkg
                for scoped in sorted(entry.iterdir()):
                    if scoped.is_dir() and _contains_sol(scoped):
                        prefix = f"{entry.name}/{scoped.name}/"
                        remappings[prefix] = f"{scoped}/"
            elif _contains_sol(entry):
                prefix = f"{entry.name}/"
                remappings[prefix] = f"{entry}/"
        return [Remapping(p, t) for p, t in sorted(remappings.items())]

    def _dependency_root_remappings(self) -> list[Remapping]:
        """Bare layout: package dirs under each Dependency_Resolver root."""
        remappings: dict[str, str] = {}
        for dep_root in self._deps.roots:
            if not dep_root.is_dir():
                continue
            for entry in sorted(dep_root.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith("@"):
                    for scoped in sorted(entry.iterdir()):
                        if scoped.is_dir() and _contains_sol(scoped):
                            prefix = f"{entry.name}/{scoped.name}/"
                            remappings.setdefault(prefix, f"{scoped}/")
                elif _contains_sol(entry):
                    prefix = f"{entry.name}/"
                    remappings.setdefault(prefix, f"{entry}/")
        return [Remapping(p, t) for p, t in sorted(remappings.items())]

    # -- pragmas ------------------------------------------------------------

    @staticmethod
    def _first_party_constraints(
        sources: list[Path],
    ) -> dict[str, list[PragmaConstraint]]:
        pragma_re = re.compile(r"pragma\s+solidity\s+([^;]+);", re.IGNORECASE)
        per_file: dict[str, list[PragmaConstraint]] = {}
        for src in sources:
            resolved = src.resolve()
            files = (
                [resolved]
                if resolved.is_file()
                else sorted(resolved.rglob("*.sol"))
            )
            for f in files:
                if not f.is_file():
                    continue
                try:
                    text = f.read_text(errors="replace")
                except OSError:  # pragma: no cover - defensive
                    continue
                constraints: list[PragmaConstraint] = []
                for m in pragma_re.finditer(text):
                    constraints.extend(parse_pragma(m.group(1)))
                if constraints:
                    per_file[str(f)] = constraints
        return per_file


def _contains_sol(directory: Path) -> bool:
    """Return True when ``directory`` holds a ``.sol`` file at any depth."""
    try:
        for _ in directory.rglob("*.sol"):
            return True
    except OSError:  # pragma: no cover - defensive
        return False
    return False
