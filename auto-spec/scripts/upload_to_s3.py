#!/usr/bin/env python3
"""
Upload chroma_db to AWS S3 for distribution.

This script prepares and uploads the vector database to S3.
It creates a manifest file with checksums and splits large files if needed.

Usage:
    python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db
"""

import argparse
import json
import hashlib
import boto3
from pathlib import Path
from typing import Optional, Dict, List
import sys


def calculate_checksum(file_path: Path) -> str:
    """Calculate SHA256 checksum of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def create_manifest(chroma_path: Path, s3_prefix: str) -> Dict:
    """Create manifest file for database distribution.
    
    Args:
        chroma_path: Path to chroma_db directory
        s3_prefix: S3 prefix for remote files
        
    Returns:
        Manifest dictionary
    """
    print("Creating manifest for chroma_db...")
    
    manifest = {
        "version": "1.0",
        "s3_prefix": s3_prefix,
        "files": []
    }
    
    if not chroma_path.exists():
        raise FileNotFoundError(f"chroma_db not found at {chroma_path}")
    
    # List all files in chroma_db
    total_size = 0
    for file_path in sorted(chroma_path.rglob("*")):
        if file_path.is_file() and not file_path.name.startswith("."):
            rel_path = file_path.relative_to(chroma_path.parent)
            file_size = file_path.stat().st_size
            checksum = calculate_checksum(file_path)
            
            manifest["files"].append({
                "path": str(rel_path),
                "checksum": checksum,
                "size": file_size,
                "s3_key": f"{s3_prefix}/{rel_path}"
            })
            
            total_size += file_size
            print(f"  {rel_path.name}: {file_size / (1024*1024):.2f}MB")
    
    manifest["total_size"] = total_size
    manifest["file_count"] = len(manifest["files"])
    
    print(f"\nTotal files: {manifest['file_count']}")
    print(f"Total size: {total_size / (1024*1024):.2f}MB")
    
    return manifest


def upload_to_s3(
    chroma_path: Path,
    bucket: str,
    prefix: str,
    profile: Optional[str] = None,
    region: Optional[str] = None,
    dry_run: bool = False
) -> bool:
    """Upload chroma_db to S3.
    
    Args:
        chroma_path: Path to chroma_db directory
        bucket: S3 bucket name
        prefix: S3 prefix (path within bucket)
        profile: AWS profile to use
        region: AWS region
        dry_run: Only show what would be uploaded
        
    Returns:
        bool: Success status
    """
    try:
        # Create S3 client
        session = boto3.Session(profile_name=profile, region_name=region)
        s3_client = session.client("s3")
        
        # Verify bucket exists
        print(f"Verifying S3 bucket: s3://{bucket}/")
        try:
            s3_client.head_bucket(Bucket=bucket)
            print("✓ Bucket accessible")
        except Exception as e:
            print(f"✗ Cannot access bucket: {e}")
            return False
        
        # Create manifest
        manifest = create_manifest(chroma_path, f"{prefix}/chroma_db")
        
        if dry_run:
            print("\n[DRY RUN] Would upload:")
            for file_info in manifest["files"]:
                print(f"  {file_info['s3_key']}")
            return True
        
        # Upload files
        print(f"\nUploading to s3://{bucket}/{prefix}/...")
        
        uploaded_count = 0
        for idx, file_info in enumerate(manifest["files"], 1):
            local_file = chroma_path.parent / file_info["path"]
            s3_key = file_info["s3_key"]
            
            try:
                file_size_mb = file_info["size"] / (1024*1024)
                print(f"  [{idx}/{len(manifest['files'])}] {local_file.name} ({file_size_mb:.2f}MB)...", end="", flush=True)
                
                s3_client.upload_file(
                    str(local_file),
                    bucket,
                    s3_key,
                    Callback=lambda bytes_transferred: None  # Silent progress
                )
                print(" ✓")
                uploaded_count += 1
            except Exception as e:
                print(f" ✗ ({e})")
        
        # Upload manifest
        manifest_key = f"{prefix}/manifest.json"
        print(f"Uploading manifest to {manifest_key}...", end="", flush=True)
        s3_client.put_object(
            Bucket=bucket,
            Key=manifest_key,
            Body=json.dumps(manifest, indent=2),
            ContentType="application/json"
        )
        print(" ✓")
        
        print(f"\n✓ Upload complete!")
        print(f"✓ Uploaded {uploaded_count}/{len(manifest['files'])} files")
        print(f"✓ Manifest: s3://{bucket}/{manifest_key}")
        
        # Print environment variable instruction
        s3_url = f"https://{bucket}.s3.amazonaws.com/{prefix}"
        print(f"\nSet this environment variable for auto-spec:")
        print(f"  export CHROMA_DB_REMOTE_URL={s3_url}")
        
        return True
    
    except Exception as e:
        print(f"Error uploading to S3: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Upload chroma_db to AWS S3 for distribution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Upload to S3
  python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db
  
  # Dry run to see what would be uploaded
  python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db --dry-run
  
  # Use specific AWS profile
  python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db --profile myprofile
        """
    )
    
    parser.add_argument(
        "--bucket",
        required=True,
        help="S3 bucket name"
    )
    parser.add_argument(
        "--prefix",
        default="auto-spec-db",
        help="S3 prefix/path (default: auto-spec-db)"
    )
    parser.add_argument(
        "--profile",
        help="AWS profile to use"
    )
    parser.add_argument(
        "--region",
        help="AWS region"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be uploaded"
    )
    parser.add_argument(
        "--chroma-path",
        default="erc20_pairs_final/chroma_db",
        help="Path to chroma_db directory"
    )
    
    args = parser.parse_args()
    
    chroma_path = Path(args.chroma_path)
    if not chroma_path.exists():
        print(f"Error: chroma_db not found at {chroma_path}")
        sys.exit(1)
    
    success = upload_to_s3(
        chroma_path=chroma_path,
        bucket=args.bucket,
        prefix=args.prefix,
        profile=args.profile,
        region=args.region,
        dry_run=args.dry_run
    )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
