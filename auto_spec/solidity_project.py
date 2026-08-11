"""Minimal Solidity project loading: local imports plus Foundry remappings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

IMPORT_PATTERN = re.compile(r"\bimport\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']\s*;")
CONTRACT_PATTERN = re.compile(r"\b(?:abstract\s+)?contract\s+([A-Za-z_]\w*)")


@dataclass(frozen=True)
class SolidityProject:
    root: Path
    entrypoint: Path
    sources: tuple[Path, ...]
    source_text: str
    unresolved_imports: tuple[str, ...]


def load_remappings(path: str | Path | None) -> dict[str, Path]:
    if path is None or not Path(path).is_file():
        return {}
    mappings = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        prefix, target = (part.strip() for part in line.split("=", 1))
        if prefix:
            mappings[prefix] = Path(target)
    return mappings


def _resolve_import(import_path: str, source: Path, root: Path, remappings: dict[str, Path]) -> Path | None:
    if import_path.startswith("."):
        candidate = (source.parent / import_path).resolve()
    else:
        target = next((path for prefix, path in remappings.items() if import_path.startswith(prefix)), None)
        if target is None:
            return None
        prefix = next(prefix for prefix in remappings if import_path.startswith(prefix))
        candidate = (root / target / import_path.removeprefix(prefix)).resolve()
    return candidate if candidate.is_file() else None


def load_project(
    entrypoint: str | Path,
    project_root: str | Path | None = None,
    remappings_file: str | Path | None = None,
) -> SolidityProject:
    entrypoint = Path(entrypoint).resolve()
    if not entrypoint.is_file():
        raise FileNotFoundError(f"Contract file not found: {entrypoint}")
    root = Path(project_root).resolve() if project_root else entrypoint.parent
    remappings = load_remappings(remappings_file or root / "remappings.txt")
    pending, visited, sources, missing = [entrypoint], set(), [], []

    while pending:
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        text = source.read_text(encoding="utf-8", errors="replace")
        sources.append(source)
        for import_path in IMPORT_PATTERN.findall(text):
            resolved = _resolve_import(import_path, source, root, remappings)
            if resolved:
                pending.append(resolved)
            else:
                missing.append(import_path)

    source_text = "\n\n".join(
        f"// SOURCE: {source.relative_to(root) if source.is_relative_to(root) else source}\n"
        + source.read_text(encoding="utf-8", errors="replace")
        for source in sources
    )
    return SolidityProject(root, entrypoint, tuple(sources), source_text, tuple(sorted(set(missing))))


def detect_contract_name(source: str, fallback: str) -> str:
    """Best-guess the contract to audit.

    Preference order:
      1. A contract whose name matches the file stem (most .sol files follow
         one-contract-per-file naming) — takes priority over the first match.
      2. The largest contract by source span — in multi-contract files the
         target is usually the biggest, and the first match is often an
         interface/library/helper.
      3. The fallback (file stem).
    """
    matches = list(CONTRACT_PATTERN.finditer(source))
    if not matches:
        return fallback

    if fallback:
        for m in matches:
            if m.group(1) == fallback:
                return fallback

    if len(matches) == 1:
        return matches[0].group(1)

    best_name, best_size = matches[0].group(1), 0
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(source)
        if end - m.start() > best_size:
            best_name, best_size = m.group(1), end - m.start()
    return best_name
