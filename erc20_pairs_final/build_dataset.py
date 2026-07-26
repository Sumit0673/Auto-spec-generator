#!/usr/bin/env python3
"""
build_dataset.py
────────────────
Walks every numbered pair folder in erc20_pairs_final/, strips boilerplate
comments from Solidity (.sol) and Certora spec (.spec) files, then writes a
single clean JSON dataset: dataset.json

Run:
    python3 build_dataset.py
Output:
    dataset.json   — one record per pair
    dataset/       — mirrored folder structure with cleaned files
"""

import json
import os
import re
import shutil
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent           # erc20_pairs_final/
OUT_JSON = ROOT / "dataset.json"
OUT_DIR  = ROOT / "dataset"

# Manifest metadata (contract name, folder label) keyed by folder prefix
MANIFEST = {
    "01_AaveTokenV3_erc20":               ("AaveTokenV3",           "AaveTokenV3_erc20"),
    "02_AaveTokenV3_community":           ("AaveTokenV3",           "AaveTokenV3_community"),
    "03_AaveTokenV3_delegate":            ("AaveTokenV3",           "AaveTokenV3_delegate"),
    "04_AaveTokenV3_general":             ("AaveTokenV3",           "AaveTokenV3_general"),
    "05_StakedAaveV3_erc20":              ("StakedAaveV3",          "StakedAaveV3_erc20"),
    "06_StakedAaveV3_delegate":           ("StakedAaveV3",          "StakedAaveV3_delegate"),
    "07_StakedAaveV3_community":          ("StakedAaveV3",          "StakedAaveV3_community"),
    "08_StakedAaveV3_general":            ("StakedAaveV3",          "StakedAaveV3_general"),
    "09_StakedAaveV3_invariants":         ("StakedAaveV3",          "StakedAaveV3_invariants"),
    "10_GhoToken":                        ("GhoToken",              "GhoToken"),
    "11_GhoVariableDebtToken":            ("GhoVariableDebtToken",  "GhoVariableDebtToken"),
    "12_GhoVariableDebtToken_summarized": ("GhoVariableDebtToken",  "GhoVariableDebtToken_summarized"),
    "13_GhoAToken":                       ("GhoAToken",             "GhoAToken"),
    "14_Gho_erc20_helper":               ("GhoTokenHarness",       "Gho_erc20_helper"),
    "15_AStETH_StableDebtToken":         ("StableDebtToken",       "AStETH_StableDebtToken"),
    "16_AStETH_VariableDebtToken":       ("VariableDebtToken",     "AStETH_VariableDebtToken"),
    "17_StakedAaveV1_5_allProps":        ("StakedAaveV3",          "StakedAaveV1_5_allProps"),
    "18_StakedAaveV1_5_invariants":      ("StakedAaveV3",          "StakedAaveV1_5_invariants"),
    "19_Examples_ERC20Full":             ("ERC20",                 "Examples_ERC20Full"),
    "20_Examples_ERC4626":              ("ERC4626",               "Examples_ERC4626"),
}

# ── Stripping helpers ─────────────────────────────────────────────────────────

# Matches block comments: /* ... */  (including multi-line)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# Matches single-line // comments (the whole line if it starts with //)
_LINE_COMMENT_FULL = re.compile(r"^\s*//.*\n?", re.MULTILINE)
# Trailing // comments on code lines
_INLINE_COMMENT = re.compile(r"\s*//.*$", re.MULTILINE)
# Collapse 3+ blank lines → 1
_MULTI_BLANK = re.compile(r"\n{3,}")
# SPDX line (pure boilerplate)
_SPDX = re.compile(r"^\s*//\s*SPDX-License-Identifier:.*\n?", re.MULTILINE)


def strip_sol(src: str) -> str:
    """Remove comments and blank noise from Solidity source."""
    src = _SPDX.sub("", src)
    src = _BLOCK_COMMENT.sub("", src)
    src = _LINE_COMMENT_FULL.sub("", src)
    src = _INLINE_COMMENT.sub("", src)
    src = _MULTI_BLANK.sub("\n\n", src)
    return src.strip()


# CVL spec block-comment tags like @Rule, @Description, @Formula, @Notes, @Link
# are KEPT because they carry semantic meaning for the verifier.
# We convert the outer /* */ wrappers into per-line // comments.
_CVL_OUTER_COMMENT = re.compile(
    r"/\*.*?\*/",
    re.DOTALL,
)


def _keep_cvl_body(m: re.Match) -> str:
    """Convert a /* */ block comment into // prefixed lines, one per line."""
    # Remove the delimiters, then strip leading * decoration on each line
    inner = m.group(0)[2:-2]          # strip /* and */
    result_lines = []
    for line in inner.splitlines():
        line = re.sub(r"^\s*\*\s?", "", line)   # strip leading *
        line = line.rstrip()
        if line.strip():                          # skip fully blank lines
            result_lines.append(f"// {line}")
    return "\n".join(result_lines)


def strip_spec(src: str) -> str:
    """
    Clean CVL spec:
    - Convert /* */ block comments to per-line // prefixed text (preserving
      semantic annotations like @Rule / @Description / @Formula)
    - Remove SPDX lines
    - Collapse 3+ blank lines → 1
    """
    src = _SPDX.sub("", src)
    src = _CVL_OUTER_COMMENT.sub(_keep_cvl_body, src)
    src = _MULTI_BLANK.sub("\n\n", src)
    return src.strip()


# ── File discovery ────────────────────────────────────────────────────────────

def find_sol_files(contracts_dir: Path) -> list[Path]:
    """Recursively find all .sol files under contracts_dir."""
    return sorted(contracts_dir.rglob("*.sol"))


def find_spec_files(spec_dir: Path) -> list[Path]:
    """Recursively find all .spec files under spec_dir."""
    return sorted(spec_dir.rglob("*.spec"))


# Some pairs use 'spec/' and some 'specs/' — handle both
def resolve_spec_dir(pair_dir: Path) -> Path | None:
    for name in ("spec", "specs"):
        d = pair_dir / name
        if d.is_dir():
            return d
    return None


# ── Main builder ──────────────────────────────────────────────────────────────

def build():
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    records = []
    pair_dirs = sorted(
        d for d in ROOT.iterdir()
        if d.is_dir() and re.match(r"^\d{2}_", d.name)
    )

    for pair_dir in pair_dirs:
        folder_name = pair_dir.name
        contract_name, label = MANIFEST.get(folder_name, (folder_name, folder_name))

        contracts_dir = pair_dir / "contracts"
        spec_dir      = resolve_spec_dir(pair_dir)

        if not contracts_dir.is_dir():
            print(f"[WARN] No contracts/ in {folder_name}, skipping.")
            continue
        if spec_dir is None:
            print(f"[WARN] No spec/ or specs/ in {folder_name}, skipping.")
            continue

        sol_files  = find_sol_files(contracts_dir)
        spec_files = find_spec_files(spec_dir)

        if not sol_files:
            print(f"[WARN] No .sol files found in {folder_name}/contracts/, skipping.")
            continue
        if not spec_files:
            print(f"[WARN] No .spec files found in {folder_name}/spec*, skipping.")
            continue

        # ── Clean contracts ──────────────────────────────────────────
        contract_entries = []
        out_contracts_dir = OUT_DIR / label / "contracts"
        out_contracts_dir.mkdir(parents=True, exist_ok=True)

        for sol_path in sol_files:
            raw  = sol_path.read_text(encoding="utf-8", errors="replace")
            clean = strip_sol(raw)
            rel   = sol_path.relative_to(contracts_dir)
            out_path = out_contracts_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(clean, encoding="utf-8")
            contract_entries.append({
                "filename": sol_path.name,
                "relative_path": str(rel),
                "clean_source": clean,
            })

        # ── Clean specs ──────────────────────────────────────────────
        spec_entries = []
        out_spec_dir = OUT_DIR / label / "spec"
        out_spec_dir.mkdir(parents=True, exist_ok=True)

        for spec_path in spec_files:
            raw   = spec_path.read_text(encoding="utf-8", errors="replace")
            clean = strip_spec(raw)
            rel   = spec_path.relative_to(spec_dir)
            out_path = out_spec_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(clean, encoding="utf-8")
            spec_entries.append({
                "filename": spec_path.name,
                "relative_path": str(rel),
                "clean_source": clean,
            })

        record = {
            "id":            folder_name,
            "label":         label,
            "contract_name": contract_name,
            "contracts":     contract_entries,
            "specs":         spec_entries,
        }
        records.append(record)
        print(f"[OK]  {folder_name}  →  {len(sol_files)} contract(s), {len(spec_files)} spec(s)")

    OUT_JSON.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── Stats summary ────────────────────────────────────────────────────────
    stats = {
        "total_pairs": len(records),
        "total_contract_files": sum(len(r["contracts"]) for r in records),
        "total_spec_files": sum(len(r["specs"]) for r in records),
        "pairs": [
            {
                "id": r["id"],
                "contract_name": r["contract_name"],
                "contract_files": [c["filename"] for c in r["contracts"]],
                "spec_files": [s["filename"] for s in r["specs"]],
                "contract_chars": sum(len(c["clean_source"]) for c in r["contracts"]),
                "spec_chars": sum(len(s["clean_source"]) for s in r["specs"]),
            }
            for r in records
        ],
    }
    stats_path = ROOT / "dataset_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

    # ── README inside dataset/ ───────────────────────────────────────────────
    readme = """# erc20_pairs_final — Cleaned Dataset

Generated by `build_dataset.py`. Contains cleaned Solidity contract sources and
Certora Verification Language (CVL) spec sources for 20 real ERC20-focused
formal-verification pairs.

## Structure

```
dataset/
  <label>/
    contracts/   — cleaned .sol source files (boilerplate/comments stripped)
    spec/        — cleaned .spec files (/* */ blocks converted to // lines)
```

## dataset.json

Flat JSON array. Each element:
```json
{
  "id":            "01_AaveTokenV3_erc20",
  "label":         "AaveTokenV3_erc20",
  "contract_name": "AaveTokenV3",
  "contracts": [{"filename": "...", "relative_path": "...", "clean_source": "..."}],
  "specs":     [{"filename": "...", "relative_path": "...", "clean_source": "..."}]
}
```

## Loader

```python
from load_dataset import load_pairs, iter_tuples

for contract_src, spec_src, name in iter_tuples():
    print(name)
```

See `load_dataset.py --help` for CLI usage.
"""
    (OUT_DIR / "README.md").write_text(readme, encoding="utf-8")

    print(f"\n✓ Wrote {len(records)} records → {OUT_JSON}")
    print(f"✓ Cleaned files mirrored  → {OUT_DIR}/")
    print(f"✓ Stats summary           → {stats_path}")


if __name__ == "__main__":
    build()
