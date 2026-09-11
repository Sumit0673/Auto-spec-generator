"""
Shared utilities for spec_pipeline.
"""

from __future__ import annotations

from pathlib import Path


def read_source(source_path: str | Path) -> str:
    """Read Solidity source code from a file or directory. Shared by all stages."""
    source_path = Path(source_path)
    if source_path.is_file():
        return source_path.read_text(errors="ignore")

    parts = []
    for sol_file in sorted(source_path.rglob("*.sol")):
        try:
            parts.append(sol_file.read_text(errors="ignore"))
        except OSError:
            continue
    return "\n\n".join(parts)