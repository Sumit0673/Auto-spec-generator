#!/usr/bin/env python3
"""
CLI entry point for Auto-Spec.
"""

import sys
import argparse
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from auto_spec import SpecGenerator, __version__
from auto_spec.config import get_config
from auto_spec.vector_db import VectorDBManager


def cmd_generate(args):
    """Generate CVL specification."""
    try:
        generator = SpecGenerator()
        
        spec = generator.generate(
            contract_path=args.contract,
            query=args.query,
            top_k=args.top_k,
            output_path=args.output
        )
        
        if not args.output and not args.quiet:
            print("\n" + "="*80)
            print("GENERATED CVL SPECIFICATION:")
            print("="*80)
            print(spec)
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_setup(args):
    """Download and setup vector database."""
    try:
        config = get_config()
        db_manager = VectorDBManager(config)
        
        if config.CHROMA_DB_PATH.exists():
            print(f"✓ Vector database already exists at: {config.CHROMA_DB_PATH}")
            return
        
        if config.CHROMA_DB_REMOTE_URL:
            print(f"Downloading vector database...")
            if db_manager.download_db():
                print("✓ Setup complete!")
            else:
                print("Error: Failed to download database")
                sys.exit(1)
        else:
            print("Error: CHROMA_DB_REMOTE_URL not configured")
            print("Please set CHROMA_DB_REMOTE_URL environment variable")
            sys.exit(1)
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_config(args):
    """Display current configuration."""
    config = get_config()
    
    print("Auto-Spec Configuration")
    print("="*50)
    print(f"LLM Provider: {config.LLM_PROVIDER}")
    print(f"LLM Model: {config.LLM_MODEL}")
    print(f"Embedding Model: {config.EMBEDDING_MODEL}")
    print(f"Chroma DB Path: {config.CHROMA_DB_PATH}")
    print(f"Output Directory: {config.OUTPUT_DIR}")
    print(f"Top K Results: {config.TOP_K_RESULTS}")
    print(f"API Key Set: {bool(config.LLM_API_KEY)}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Auto-Spec: Automated CVL Specification Generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate spec from contract
  auto-spec generate path/to/MyToken.sol
  
  # Generate with custom query
  auto-spec generate path/to/MyToken.sol --query "ERC20 transfer rules"
  
  # Save to specific path
  auto-spec generate path/to/MyToken.sol -o output/MyToken.spec
  
  # Setup vector database
  auto-spec setup
  
  # Show configuration
  auto-spec config
        """
    )
    
    parser.add_argument("--version", action="version", version=f"Auto-Spec {__version__}")
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Generate command
    gen_parser = subparsers.add_parser("generate", help="Generate CVL specification")
    gen_parser.add_argument("contract", help="Path to Solidity contract file")
    gen_parser.add_argument("--query", "-q", help="Search query for reference specs")
    gen_parser.add_argument("--top_k", type=int, default=3, help="Number of reference specs (default: 3)")
    gen_parser.add_argument("--output", "-o", help="Output path for .spec file")
    gen_parser.add_argument("--quiet", action="store_true", help="Don't print spec to stdout")
    gen_parser.set_defaults(func=cmd_generate)
    
    # Setup command
    setup_parser = subparsers.add_parser("setup", help="Setup vector database")
    setup_parser.set_defaults(func=cmd_setup)
    
    # Config command
    config_parser = subparsers.add_parser("config", help="Show configuration")
    config_parser.set_defaults(func=cmd_config)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    args.func(args)


if __name__ == "__main__":
    main()
