"""
CLI entry point for solidity_graph analyzer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add parent directory to path for absolute imports
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solidity_graph.analyzer import analyze_path
from solidity_graph.rag_exporter import export_rag_chunks


def main():
    parser = argparse.ArgumentParser(
        description="Solidity Function Graph Analyzer for RAG-based spec generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m solidity_graph analyze ./contracts/Token.sol --formats json graphml rag
  python -m solidity_graph analyze ./my-project --formats json --output-dir ./analysis
  python -m solidity_graph analyze ./contracts --formats rag --output-dir ./rag_data
        """
    )

    parser.add_argument(
        "path",
        help="Path to .sol file or directory containing Solidity files"
    )
    parser.add_argument(
        "--formats", "-f",
        nargs="+",
        choices=["json", "graphml", "rag"],
        default=["json"],
        help="Output formats (default: json)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        help="Output directory (default: same as input path)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )

    args = parser.parse_args()

    path = Path(args.path).resolve()
    if not path.exists():
        print(f"Error: Path does not exist: {path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None

    try:
        graph = analyze_path(
            path,
            output_dir=output_dir,
            formats=args.formats,
        )

        print("\nAnalysis complete!")
        print(f"Contracts: {len(graph.contracts)}")
        total_funcs = sum(len(c.functions) for c in graph.contracts.values())
        print(f"Functions: {total_funcs}")
        print(f"Call graph edges: {graph.call_graph.number_of_edges()}")
        print(f"Inheritance edges: {graph.inheritance_graph.number_of_edges()}")

        # Print contract summary
        for cname, cinfo in graph.contracts.items():
            print(f"  {cname} ({cinfo.kind}): {len(cinfo.functions)} functions, {len(cinfo.state_variables)} state vars, {len(cinfo.events)} events")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()