#!/usr/bin/env python3
"""
Create and upload chroma_db as GitHub Release artifact.

This script packages chroma_db as a compressed archive and can be used
as part of a GitHub Actions workflow to create releases.

Usage:
    python scripts/create_release.py --version 1.0.0 --output chroma_db.tar.gz
"""

import argparse
import tarfile
import hashlib
import json
from pathlib import Path
from typing import Optional
import sys


def calculate_checksum(file_path: Path) -> str:
    """Calculate SHA256 checksum of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def create_tarball(
    chroma_path: Path,
    output_path: Path,
    compression: str = "gz"
) -> bool:
    """Create compressed tarball of chroma_db.
    
    Args:
        chroma_path: Path to chroma_db directory
        output_path: Output archive path
        compression: Compression type (gz, bz2, xz)
        
    Returns:
        bool: Success status
    """
    try:
        if not chroma_path.exists():
            print(f"Error: {chroma_path} not found")
            return False
        
        mode = f"w:{compression}"
        print(f"Creating {output_path.name}...")
        
        with tarfile.open(output_path, mode) as tar:
            tar.add(chroma_path, arcname="chroma_db")
        
        size_mb = output_path.stat().st_size / (1024*1024)
        print(f"✓ Created {output_path.name} ({size_mb:.2f}MB)")
        
        return True
    
    except Exception as e:
        print(f"Error creating tarball: {e}")
        return False


def create_release_metadata(
    version: str,
    chroma_path: Path,
    archive_path: Path,
    output_path: Optional[Path] = None
) -> dict:
    """Create metadata file for release.
    
    Args:
        version: Version number
        chroma_path: Path to chroma_db
        archive_path: Path to created archive
        output_path: Where to save metadata
        
    Returns:
        Metadata dictionary
    """
    print("Creating release metadata...")
    
    archive_checksum = calculate_checksum(archive_path)
    archive_size = archive_path.stat().st_size
    
    metadata = {
        "version": version,
        "release_date": Path.cwd().stat().st_mtime,
        "archive": {
            "filename": archive_path.name,
            "size": archive_size,
            "size_mb": archive_size / (1024*1024),
            "checksum_sha256": archive_checksum,
            "compression": "gzip"
        },
        "contents": {
            "path": "chroma_db",
            "description": "Vector database with 20+ verified CVL specifications"
        },
        "installation": "tar -xzf chroma_db.tar.gz && mv chroma_db ~/.auto-spec/",
        "usage": "export CHROMA_DB_PATH=~/.auto-spec/chroma_db"
    }
    
    if output_path:
        output_path.write_text(json.dumps(metadata, indent=2))
        print(f"✓ Metadata saved to {output_path.name}")
    
    return metadata


def main():
    parser = argparse.ArgumentParser(
        description="Create GitHub Release artifact for chroma_db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create release archive
  python scripts/create_release.py --version 1.0.0
  
  # Create with metadata
  python scripts/create_release.py --version 1.0.0 --metadata
  
  # Custom output path
  python scripts/create_release.py --version 1.0.0 -o releases/chroma_db_v1.0.tar.gz
        """
    )
    
    parser.add_argument(
        "--version",
        required=True,
        help="Release version (e.g., 1.0.0)"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output archive path"
    )
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="Create metadata.json file"
    )
    parser.add_argument(
        "--chroma-path",
        default="erc20_pairs_final/chroma_db",
        help="Path to chroma_db directory"
    )
    parser.add_argument(
        "--compression",
        default="gz",
        choices=["gz", "bz2", "xz"],
        help="Compression type"
    )
    
    args = parser.parse_args()
    
    chroma_path = Path(args.chroma_path)
    
    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path(f"chroma_db_v{args.version}.tar.{args.compression}")
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Create tarball
    if not create_tarball(chroma_path, output_path, args.compression):
        sys.exit(1)
    
    # Create metadata if requested
    if args.metadata:
        metadata_path = output_path.parent / "metadata.json"
        create_release_metadata(
            args.version,
            chroma_path,
            output_path,
            metadata_path
        )
    
    # Print release info
    print(f"\n{'='*60}")
    print("Release Information:")
    print(f"{'='*60}")
    print(f"Version: {args.version}")
    print(f"Archive: {output_path}")
    print(f"Size: {output_path.stat().st_size / (1024*1024):.2f}MB")
    print(f"Checksum: {calculate_checksum(output_path)}")
    print(f"\nFor GitHub Release, upload: {output_path}")


if __name__ == "__main__":
    main()
