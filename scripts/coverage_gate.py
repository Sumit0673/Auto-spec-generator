#!/usr/bin/env python3
"""Coverage_Gate: enforce the ``spec_pipeline`` statement-coverage floor.

Requirement 10.6: IF measured statement coverage of ``spec_pipeline`` falls
below 80 percent, THEN the Coverage_Gate exits with code 1 and lists the modules
below the floor. Requirement 10.9: the floor lives in the version-controlled
Project_Manifest (``pyproject.toml`` ``[tool.coverage.report] fail_under``).

This gate runs pytest under coverage against ``spec_pipeline`` with the floor
enforced by ``--cov-fail-under``, then reads the coverage data to list every
``spec_pipeline`` module whose statement coverage is below the floor. A breach
(either pytest's own ``--cov-fail-under`` failure or any per-module shortfall)
exits with code 1; a clean run exits 0.

CI runs this real gate. The unit test ``tests/unit/test_coverage_gate.py`` only
asserts the gate is configured and present, and does NOT require the 80 percent
floor to be met in that run.

Usage:
    python scripts/coverage_gate.py [pytest args...]

Exit codes:
    0   coverage at or above the floor for spec_pipeline overall and per module.
    1   coverage below the floor (modules below the floor are listed on stdout).
    2   the coverage data could not be produced or read.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:  # tomllib is stdlib on 3.11+; fall back to tomli if present.
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter version
    import tomli as _toml  # type: ignore[no-redef]

# Exit codes (single source of truth for this tool).
EXIT_CLEAN = 0
EXIT_BELOW_FLOOR = 1
EXIT_DATA_UNAVAILABLE = 2

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
PACKAGE = "spec_pipeline"


def read_floor() -> int:
    """Return the configured statement-coverage floor from the Project_Manifest.

    Reads ``[tool.coverage.report] fail_under`` from ``pyproject.toml`` so the
    floor has exactly one version-controlled source (Requirement 10.9).
    """
    with open(PYPROJECT_PATH, "rb") as fh:
        data = _toml.load(fh)
    return int(data["tool"]["coverage"]["report"]["fail_under"])


def run_coverage(floor: int, pytest_args: list[str]) -> int:
    """Run pytest under coverage with the floor enforced; return pytest's code."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        f"--cov={PACKAGE}",
        f"--cov-fail-under={floor}",
        "--cov-report=term-missing",
        *pytest_args,
    ]
    completed = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return completed.returncode


def modules_below_floor(floor: int) -> list[tuple[str, float]]:
    """Return ``(module, percent)`` for each ``spec_pipeline`` module below floor.

    Reads the ``.coverage`` data written by the pytest run via coverage.py's
    API. Returns an empty list when no module is below the floor.
    """
    try:
        from coverage import Coverage
    except ModuleNotFoundError:  # pragma: no cover - coverage is a test dep
        return []

    cov = Coverage(data_file=str(REPO_ROOT / ".coverage"))
    cov.load()
    data = cov.get_data()

    below: list[tuple[str, float]] = []
    for filename in data.measured_files():
        if PACKAGE not in Path(filename).parts:
            continue
        analysis = cov.analysis2(filename)
        statements = set(analysis[1])
        missing = set(analysis[3])
        total = len(statements)
        if total == 0:
            continue
        covered = total - len(missing)
        percent = 100.0 * covered / total
        if percent < floor:
            below.append((filename, percent))
    return sorted(below)


def main(argv: list[str]) -> int:
    floor = read_floor()
    pytest_code = run_coverage(floor, argv)

    below = modules_below_floor(floor)
    if below:
        print(f"\nCoverage_Gate: modules below the {floor}% floor:")
        for filename, percent in below:
            rel = Path(filename).relative_to(REPO_ROOT) if str(filename).startswith(
                str(REPO_ROOT)
            ) else Path(filename)
            print(f"  {rel}: {percent:.1f}%")

    if pytest_code != 0 or below:
        return EXIT_BELOW_FLOOR
    print(f"Coverage_Gate: {PACKAGE} statement coverage meets the {floor}% floor.")
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
