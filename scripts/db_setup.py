#!/usr/bin/env python3
"""
Database setup and management script.

This script prepares the vector database for use with auto-spec.
It can download pre-built databases or create one from local specs.
"""

import argparse
import json
from pathlib import Path
from auto_spec.vector_db import VectorDBManager
from auto_spec.config import get_config


def download_db(args):
    """Download pre-built vector database."""
    config = get_config()
    manager = VectorDBManager(config)
    
    print("Downloading vector database...")
    if manager.download_db():
        print("✓ Database downloaded successfully!")
    else:
        print("✗ Failed to download database")


def create_manifest(args):
    """Create manifest file for database distribution."""
    chroma_path = Path(args.chroma_db)
    
    manifest = {
        "version": "1.0",
        "files": []
    }
    
    # List all files in chroma_db
    for file_path in chroma_path.rglob("*"):
        if file_path.is_file():
            rel_path = file_path.relative_to(chroma_path)
            # Calculate checksum
            import hashlib
            with open(file_path, "rb") as f:
                checksum = hashlib.sha256(f.read()).hexdigest()
            
            manifest["files"].append({
                "path": str(rel_path),
                "checksum": checksum,
                "size": file_path.stat().st_size
            })
    
    # Save manifest
    manifest_path = chroma_path / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    
    print(f"✓ Manifest created: {manifest_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-Spec Database Management"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Download command
    download_parser = subparsers.add_parser("download", help="Download vector database")
    download_parser.set_defaults(func=download_db)
    
    # Create manifest command
    manifest_parser = subparsers.add_parser("manifest", help="Create database manifest")
    manifest_parser.add_argument("chroma_db", help="Path to chroma_db directory")
    manifest_parser.set_defaults(func=create_manifest)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    args.func(args)


if __name__ == "__main__":
    main()
