#!/usr/bin/env python3
"""
load_dataset.py
───────────────
Simple loader for the cleaned contract/spec dataset produced by build_dataset.py.

Usage examples
──────────────
  # Load all pairs
  from load_dataset import load_pairs
  pairs = load_pairs()

  # Iterate
  for p in pairs:
      print(p.contract_name, "→", [s["filename"] for s in p.specs])

  # Filter by contract name
  pairs = load_pairs(contract_name="GhoToken")

  # Get raw dict list instead of objects
  pairs = load_pairs(as_dicts=True)

CLI quick-look:
  python3 load_dataset.py
  python3 load_dataset.py --name AaveTokenV3
  python3 load_dataset.py --id 01_AaveTokenV3_erc20
  python3 load_dataset.py --list
"""

from __future__ import annotations

import argparse
import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DATASET_JSON = Path(__file__).parent / "dataset.json"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ContractEntry:
    filename:      str
    relative_path: str
    clean_source:  str


@dataclass
class SpecEntry:
    filename:      str
    relative_path: str
    clean_source:  str


@dataclass
class Pair:
    id:            str           # folder name, e.g. "01_AaveTokenV3_erc20"
    label:         str           # short label,  e.g. "AaveTokenV3_erc20"
    contract_name: str           # primary contract,  e.g. "AaveTokenV3"
    contracts:     list[ContractEntry] = field(default_factory=list)
    specs:         list[SpecEntry]     = field(default_factory=list)

    # ── Convenience accessors ─────────────────────────────────────────────────

    def main_contract(self) -> ContractEntry | None:
        """Return the harness .sol if present, else the first .sol file."""
        harness = [c for c in self.contracts if "harness" in c.filename.lower()]
        return harness[0] if harness else (self.contracts[0] if self.contracts else None)

    def main_spec(self) -> SpecEntry | None:
        return self.specs[0] if self.specs else None

    def as_tuple(self) -> tuple[str, str, str]:
        """(contract_source, spec_source, contract_name)"""
        c = self.main_contract()
        s = self.main_spec()
        return (
            c.clean_source if c else "",
            s.clean_source if s else "",
            self.contract_name,
        )


# ── Loader ────────────────────────────────────────────────────────────────────

def load_pairs(
    *,
    path: Path | str = DATASET_JSON,
    contract_name: str | None = None,
    pair_id: str | None = None,
    as_dicts: bool = False,
) -> list[Pair] | list[dict]:
    """
    Load the dataset.

    Parameters
    ----------
    path          : path to dataset.json  (default: auto-detected)
    contract_name : if given, filter to pairs whose contract_name matches
                    (case-insensitive substring match)
    pair_id       : if given, return only the pair with this folder id
    as_dicts      : if True, return raw dicts instead of Pair objects
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    if as_dicts:
        results = data
    else:
        results = [
            Pair(
                id=r["id"],
                label=r["label"],
                contract_name=r["contract_name"],
                contracts=[ContractEntry(**c) for c in r["contracts"]],
                specs=[SpecEntry(**s) for s in r["specs"]],
            )
            for r in data
        ]

    # Filter
    if contract_name:
        cn = contract_name.lower()
        if as_dicts:
            results = [r for r in results if cn in r["contract_name"].lower()]
        else:
            results = [p for p in results if cn in p.contract_name.lower()]

    if pair_id:
        if as_dicts:
            results = [r for r in results if r["id"] == pair_id]
        else:
            results = [p for p in results if p.id == pair_id]

    return results


def iter_tuples(
    **kwargs,
) -> Iterator[tuple[str, str, str]]:
    """
    Yield (contract_source, spec_source, contract_name) for every pair.
    Accepts same kwargs as load_pairs().
    """
    for pair in load_pairs(**kwargs):
        yield pair.as_tuple()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="Inspect the erc20_pairs_final dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list",   action="store_true", help="List all pairs (id, contract_name, #contracts, #specs)")
    parser.add_argument("--name",   metavar="NAME",      help="Filter by contract name (substring, case-insensitive)")
    parser.add_argument("--id",     metavar="ID",        help="Show a single pair by folder id")
    parser.add_argument("--source", action="store_true", help="Print cleaned contract + spec source for matched pair(s)")
    parser.add_argument("--json",   dest="as_json", action="store_true", help="Dump matched records as JSON")
    args = parser.parse_args()

    pairs = load_pairs(contract_name=args.name, pair_id=args.id)

    if args.as_json:
        print(json.dumps([
            {
                "id": p.id,
                "contract_name": p.contract_name,
                "contracts": [{"filename": c.filename, "source": c.clean_source} for c in p.contracts],
                "specs": [{"filename": s.filename, "source": s.clean_source} for s in p.specs],
            }
            for p in pairs
        ], indent=2))
        return

    if args.list or (not args.source and not args.id):
        print(f"{'ID':<40} {'Contract':<30} {'#sol':>4} {'#spec':>5}")
        print("─" * 84)
        for p in pairs:
            print(f"{p.id:<40} {p.contract_name:<30} {len(p.contracts):>4} {len(p.specs):>5}")
        print(f"\nTotal: {len(pairs)} pair(s)")
        return

    for p in pairs:
        print(f"\n{'═'*72}")
        print(f"  Pair      : {p.id}")
        print(f"  Label     : {p.label}")
        print(f"  Contract  : {p.contract_name}")
        print(f"  Sol files : {[c.filename for c in p.contracts]}")
        print(f"  Spec files: {[s.filename for s in p.specs]}")
        if args.source:
            c, s, _ = p.as_tuple()
            print(f"\n── Contract source ({'truncated to 60 lines' if c.count(chr(10)) > 60 else 'full'}) ──")
            print("\n".join(c.splitlines()[:60]))
            print(f"\n── Spec source ({'truncated to 60 lines' if s.count(chr(10)) > 60 else 'full'}) ──")
            print("\n".join(s.splitlines()[:60]))


if __name__ == "__main__":
    _cli()
