#!/usr/bin/env python3
"""Hygiene_Check: report version-control hygiene violations (Requirement 9).

This tool answers one question: does the tracked file set match the ignore
rules? It reports every tracked path that matches an ignore pattern and every
tracked compiled Python artifact (``*.pyc``). It never modifies the git index
on its own; a resolution is applied only after the operator confirms a choice
at an interactive prompt.

Exit codes:
    0   clean: no tracked path matches an ignore pattern and no tracked ``.pyc``.
    1   offenders found (they are listed on stdout).
    2   the git index could not be enumerated.

Usage:
    python scripts/hygiene_check.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Exit codes (single source of truth for this tool).
EXIT_CLEAN = 0
EXIT_OFFENDERS = 1
EXIT_INDEX_UNAVAILABLE = 2

REPO_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE_PATH = REPO_ROOT / ".gitignore"


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command from the repository root, capturing text output."""
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def enumerate_index() -> list[str] | None:
    """Return the tracked paths via ``git ls-files``.

    Returns None when the index cannot be enumerated (not a repository, git
    unavailable, or a nonzero exit), which the caller maps to exit code 2.
    """
    try:
        result = _run_git(["ls-files", "-z"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout
    paths = [p for p in raw.split("\0") if p]
    return paths


@dataclass(frozen=True)
class IgnorePattern:
    """One ignore pattern read from .gitignore, retaining its raw form."""

    raw: str

    @property
    def is_dir_pattern(self) -> bool:
        return self.raw.endswith("/")

    def matches(self, path: str) -> bool:
        """Report whether a tracked path is covered by this pattern.

        The matching is intentionally conservative and mirrors the subset of
        gitignore semantics used by this repository's .gitignore: directory
        prefixes (``dir/``), suffix globs (``*.ext``), and anchored or
        unanchored path fragments. It is used only for reporting.
        """
        pat = self.raw
        if pat.endswith("/"):
            prefix = pat.rstrip("/")
            # Anchored directory (e.g. "solidity_graph/__pycache__/").
            if "/" in prefix:
                return path == prefix or path.startswith(prefix + "/")
            # Unanchored directory name (e.g. "__pycache__/") matches at any
            # depth.
            segments = path.split("/")
            return prefix in segments[:-1] or (
                len(segments) >= 1 and prefix in segments and path != prefix
            ) or any(seg == prefix for seg in segments[:-1])
        if pat.startswith("*."):
            return path.endswith(pat[1:])
        if pat.startswith("*"):
            return path.endswith(pat[1:])
        # Plain path or fragment: anchored match or trailing-component match.
        if "/" in pat:
            return path == pat or path.startswith(pat + "/")
        segments = path.split("/")
        return pat in segments


def read_ignore_patterns() -> list[IgnorePattern]:
    """Read non-empty, non-comment patterns from .gitignore."""
    if not GITIGNORE_PATH.exists():
        return []
    patterns: list[IgnorePattern] = []
    for line in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(IgnorePattern(raw=stripped))
    return patterns


@dataclass
class Offenders:
    """The two categories of offenders and the pattern-to-paths mapping."""

    ignored: dict[str, list[str]] = field(default_factory=dict)
    pyc: list[str] = field(default_factory=list)

    @property
    def all_ignored_paths(self) -> list[str]:
        seen: list[str] = []
        for paths in self.ignored.values():
            for p in paths:
                if p not in seen:
                    seen.append(p)
        return seen

    def is_clean(self) -> bool:
        return not self.all_ignored_paths and not self.pyc


def find_offenders(tracked: list[str], patterns: list[IgnorePattern]) -> Offenders:
    """Classify tracked paths into ignore-matched paths and .pyc artifacts."""
    offenders = Offenders()
    for path in tracked:
        for pattern in patterns:
            if pattern.matches(path):
                offenders.ignored.setdefault(pattern.raw, []).append(path)
        if path.endswith(".pyc"):
            offenders.pyc.append(path)
    for paths in offenders.ignored.values():
        paths.sort()
    offenders.pyc.sort()
    return offenders


def report(offenders: Offenders) -> None:
    """Print the offenders and the two available resolutions per pattern."""
    ignored_total = len(offenders.all_ignored_paths)
    pyc_total = len(offenders.pyc)
    print("Hygiene_Check: tracked-file / ignore-rule audit")
    print("=" * 60)

    if offenders.ignored:
        print(f"\nTracked paths matching an ignore pattern: {ignored_total}")
        for pattern, paths in sorted(offenders.ignored.items()):
            print(f"\n  ignore pattern: {pattern}  ({len(paths)} tracked path(s))")
            for path in paths[:10]:
                print(f"    - {path}")
            if len(paths) > 10:
                print(f"    ... and {len(paths) - 10} more")
            print("    Resolutions:")
            print(
                f"      [A] Remove the ignore pattern '{pattern}' from "
                f".gitignore (changes 1 line; keeps {len(paths)} tracked "
                "path(s) tracked)."
            )
            print(
                f"      [B] Remove the {len(paths)} path(s) from the git index "
                "(git rm --cached). This is NOT recoverable from the working "
                "tree alone; the paths leave version control on the next commit."
            )
    else:
        print("\nNo tracked path matches an ignore pattern.")

    if offenders.pyc:
        print(f"\nTracked compiled Python artifacts (*.pyc): {pyc_total}")
        for path in offenders.pyc[:10]:
            print(f"    - {path}")
        if pyc_total > 10:
            print(f"    ... and {pyc_total - 10} more")

    print("\n" + "=" * 60)
    if offenders.is_clean():
        print("Result: clean.")
    else:
        print(
            f"Result: {ignored_total} ignored tracked path(s), "
            f"{pyc_total} tracked .pyc artifact(s)."
        )


def confirm(prompt: str) -> bool:
    """Ask the operator to confirm a destructive resolution."""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def apply_index_removal(paths: list[str]) -> int:
    """Remove paths from the git index after operator confirmation.

    This is the only mutating path in the tool. It is reached only through an
    explicit interactive confirmation and is never invoked automatically.
    """
    if not paths:
        return EXIT_CLEAN
    print(
        f"\nAbout to remove {len(paths)} path(s) from the git index "
        "(git rm --cached)."
    )
    print(
        "This is NOT recoverable from the working tree alone: the paths will "
        "leave version control on the next commit."
    )
    if not confirm("Proceed with index removal?"):
        print("Aborted; the git index was not modified.")
        return EXIT_OFFENDERS
    result = _run_git(["rm", "--cached", "--", *paths])
    if result.returncode != 0:
        print("git rm --cached failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return EXIT_OFFENDERS
    print(f"Removed {len(paths)} path(s) from the index.")
    return EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply_mode = "--apply" in argv

    tracked = enumerate_index()
    if tracked is None:
        print(
            "Hygiene_Check: could not enumerate the git index via "
            "`git ls-files`.",
            file=sys.stderr,
        )
        return EXIT_INDEX_UNAVAILABLE

    patterns = read_ignore_patterns()
    offenders = find_offenders(tracked, patterns)
    report(offenders)

    if offenders.is_clean():
        return EXIT_CLEAN

    if apply_mode:
        # A resolution is applied only after explicit operator confirmation.
        targets = sorted(set(offenders.all_ignored_paths) | set(offenders.pyc))
        apply_index_removal(targets)
        # Re-evaluate: a nonzero exit still reflects the pre-resolution finding
        # unless the operator resolved every offender.
        tracked_after = enumerate_index()
        if tracked_after is not None:
            offenders = find_offenders(tracked_after, patterns)
            if offenders.is_clean():
                return EXIT_CLEAN

    return EXIT_OFFENDERS


if __name__ == "__main__":
    sys.exit(main())
