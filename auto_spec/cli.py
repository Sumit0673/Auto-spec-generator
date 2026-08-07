#!/usr/bin/env python3
"""
CLI entry point for Auto-Spec.
"""

import sys
import argparse
import json
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from auto_spec import SpecGenerator, __version__
from auto_spec.config import get_config
from auto_spec.vector_db import VectorDBManager
from auto_spec.evaluation import run_evaluation


def cmd_generate(args):
    """Generate CVL specification."""
    try:
        config = get_config()
        if args.db_path:
            config.CHROMA_DB_PATH = Path(args.db_path)
        if args.temperature is not None:
            config.LLM_TEMPERATURE = args.temperature
        if args.max_tokens is not None:
            config.LLM_MAX_TOKENS = args.max_tokens
        # Re-validate config after overrides
        is_valid, error_msg = config.validate()
        if not is_valid:
            raise RuntimeError(f"Configuration error: {error_msg}")

        generator = SpecGenerator(config)
        
        spec = generator.generate(
            contract_path=args.contract,
            query=args.query,
            top_k=args.top_k,
            output_path=args.output,
            validate=args.check,
            certora_contract_name=args.contract_name,
            validation_timeout=args.validation_timeout,
            project_root=args.project_root,
            remappings_file=args.remappings,
            certora_config=args.certora_config,
            parallel=not getattr(args, 'no_parallel', False),
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
    from auto_spec.config import PROVIDER_DEFAULTS
    config = get_config()
    env_key = PROVIDER_DEFAULTS[config.LLM_PROVIDER][0]

    print("Auto-Spec Configuration")
    print("=" * 50)
    print(f"LLM Provider:   {config.LLM_PROVIDER}")
    print(f"LLM Model:      {config.LLM_MODEL}")
    print(f"LLM Base URL:   {config.LLM_BASE_URL or '(default)'}")
    print(f"API Key ({env_key}): {'✓ set' if config.LLM_API_KEY else '✗ missing'}")
    print(f"Temperature:    {config.LLM_TEMPERATURE}")
    print(f"Embedding Model: {config.EMBEDDING_MODEL}")
    print(f"Chroma DB Path: {config.CHROMA_DB_PATH}")
    print(f"Output Dir:     {config.OUTPUT_DIR}")
    print(f"Top K Results:  {config.TOP_K_RESULTS}")
    print(f"Similarity Floor: {config.MIN_RETRIEVAL_SIMILARITY}")


def cmd_evaluate(args):
    """Evaluate retrieval and, optionally, compile reference CVL specs."""
    config = get_config()
    dataset = Path(args.dataset)
    report = run_evaluation(
        dataset, VectorDBManager(config), args.top_k, args.limit, args.compile_references, args.timeout
    )
    output = Path(args.output)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    retrieval = report["retrieval"]
    print(f"Retrieval self-hit@{retrieval['top_k']}: {retrieval['hits']}/{retrieval['total']} ({retrieval['hit_rate']:.1%})")
    if "compilation" in report:
        compilation = report["compilation"]
        print(f"Reference CVL compilation: {compilation['passed']}/{compilation['total']} ({compilation['pass_rate']:.1%})")
    print(f"Report written to: {output}")


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
    gen_parser.add_argument("--check", action="store_true", help="Compile the generated CVL with Certora")
    gen_parser.add_argument("--contract-name", help="Contract name passed to Certora (defaults to the file stem)")
    gen_parser.add_argument("--validation-timeout", type=int, default=300, help="Certora compilation timeout in seconds")
    gen_parser.add_argument("--project-root", help="Solidity project root (defaults to the contract directory)")
    gen_parser.add_argument("--remappings", help="Foundry remappings.txt file")
    gen_parser.add_argument("--certora-config", help="Existing Certora .conf/.json input for --check")
    gen_parser.add_argument("--no-parallel", action="store_true", help="Disable parallel per-function drafting")
    gen_parser.add_argument("--db-path", help="Path to Chroma DB (overrides CHROMA_DB_PATH)")
    gen_parser.add_argument("--temperature", type=float, help="LLM sampling temperature (overrides config)")
    gen_parser.add_argument("--max-tokens", type=int, help="Maximum tokens for LLM generation (overrides config)")
    gen_parser.set_defaults(func=cmd_generate)
    
    # Setup command
    setup_parser = subparsers.add_parser("setup", help="Setup vector database")
    setup_parser.set_defaults(func=cmd_setup)
    
    # Config command
    config_parser = subparsers.add_parser("config", help="Show configuration")
    config_parser.set_defaults(func=cmd_config)

    eval_parser = subparsers.add_parser("evaluate", help="Evaluate retrieval and optional local Certora compilation")
    eval_parser.add_argument("--dataset", default="erc20_pairs_final/dataset.json", help="Paired Solidity/CVL dataset")
    eval_parser.add_argument("--output", default="evaluation_report.json", help="JSON report path")
    eval_parser.add_argument("--top-k", type=int, default=3, help="Retrieval cutoff")
    eval_parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N pairs")
    eval_parser.add_argument("--compile-references", action="store_true", help="Compile reference specs locally with Certora")
    eval_parser.add_argument("--timeout", type=int, default=300, help="Per-reference Certora timeout in seconds")
    eval_parser.set_defaults(func=cmd_evaluate)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    args.func(args)


if __name__ == "__main__":
    main()
