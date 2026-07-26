"""
Vector Database Management

Handles Chroma vector store initialization, loading, and downloading pre-built databases.
"""

import os
import json
import hashlib
import urllib.request
from pathlib import Path
from typing import Optional, List, Dict
from urllib.parse import urljoin

import chromadb
from sentence_transformers import SentenceTransformer

from auto_spec.config import get_config


class VectorDBManager:
    """Manages vector database operations."""
    
    def __init__(self, config=None):
        self.config = config or get_config()
        self._model = None
        self._collection = None
        self._client = None
    
    def ensure_db_exists(self) -> bool:
        """Ensure vector database exists, download if needed.
        
        Returns:
            bool: True if DB exists or successfully downloaded
        """
        if self.config.CHROMA_DB_PATH.exists():
            print(f"✓ Vector database found at {self.config.CHROMA_DB_PATH}")
            return True
        
        if self.config.CHROMA_DB_REMOTE_URL:
            print(f"Downloading vector database from {self.config.CHROMA_DB_REMOTE_URL}...")
            if self.download_db():
                return True
        
        # Provide helpful message
        print("\nVector database not found. To set it up:")
        print(f"  1. Option A - GitHub Releases (recommended):")
        print(f"     export CHROMA_DB_REMOTE_URL=https://github.com/yourusername/auto-spec/releases/download/latest/chroma_db.tar.gz")
        print(f"  2. Option B - AWS S3:")
        print(f"     export CHROMA_DB_REMOTE_URL=https://your-bucket.s3.amazonaws.com/auto-spec-db/")
        print(f"  3. Option C - Use local copy:")
        print(f"     export CHROMA_DB_PATH=/path/to/chroma_db")
        print(f"\nThen run: auto-spec setup")
        return False
    
    def download_db(self) -> bool:
        """Download pre-built vector database.
        
        Supports:
        - Direct folder URLs (with manifest.json)
        - Compressed archives (.tar.gz, .tar.bz2)
        - GitHub releases
        - S3 buckets
        
        Returns:
            bool: Success status
        """
        if not self.config.CHROMA_DB_REMOTE_URL:
            print("Error: CHROMA_DB_REMOTE_URL not configured")
            return False
        
        try:
            db_path = self.config.CHROMA_DB_PATH
            db_path.mkdir(parents=True, exist_ok=True)
            
            remote_url = self.config.CHROMA_DB_REMOTE_URL.rstrip('/')
            
            # Try to detect if it's an archive
            if remote_url.endswith(('.tar.gz', '.tar.bz2', '.tgz')):
                return self._download_archive(remote_url, db_path)
            
            # Otherwise try manifest.json approach
            return self._download_from_manifest(remote_url, db_path)
        
        except Exception as e:
            print(f"Error downloading vector database: {e}")
            return False
    
    def _download_archive(self, archive_url: str, extract_path: Path) -> bool:
        """Download and extract compressed database archive.
        
        Args:
            archive_url: URL to .tar.gz or similar archive
            extract_path: Where to extract
            
        Returns:
            bool: Success status
        """
        try:
            import tempfile
            import tarfile
            import shutil
            
            print(f"Downloading archive from {archive_url}...")
            
            # Detect compression
            if archive_url.endswith('.tar.gz') or archive_url.endswith('.tgz'):
                mode = 'r:gz'
            elif archive_url.endswith('.tar.bz2'):
                mode = 'r:bz2'
            else:
                mode = 'r'
            
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                archive_path = tmpdir_path / "chroma_db.tar"
                
                # Download archive
                urllib.request.urlretrieve(archive_url, archive_path)
                print(f"✓ Downloaded ({archive_path.stat().st_size / (1024*1024):.2f}MB)")
                
                # Extract
                print("Extracting archive...")
                with tarfile.open(archive_path, mode) as tar:
                    tar.extractall(tmpdir_path)
                
                # Move to destination
                extracted = tmpdir_path / "chroma_db"
                if extracted.exists():
                    shutil.move(str(extracted), str(extract_path))
                else:
                    # Archive might contain files directly
                    for item in tmpdir_path.iterdir():
                        if item.name != "chroma_db.tar" and item.is_dir():
                            shutil.move(str(item), str(extract_path))
                            break
            
            print("✓ Vector database extracted successfully!")
            return True
        
        except Exception as e:
            print(f"Error extracting archive: {e}")
            return False
    
    def _download_from_manifest(self, base_url: str, db_path: Path) -> bool:
        """Download database files using manifest.json.
        
        Args:
            base_url: Base URL to chroma_db directory
            db_path: Where to save files
            
        Returns:
            bool: Success status
        """
        try:
            # Try to download manifest
            manifest_url = f"{base_url}/manifest.json"
            manifest_path = db_path / "manifest.json"
            
            print(f"Downloading manifest from {manifest_url}...")
            urllib.request.urlretrieve(manifest_url, manifest_path)
            
            with open(manifest_path) as f:
                manifest = json.load(f)
            
            # Download database files
            total_size = manifest.get("total_size", 0)
            print(f"Downloading {manifest['file_count']} files ({total_size / (1024*1024):.2f}MB)...")
            
            for idx, file_info in enumerate(manifest.get("files", []), 1):
                file_url = f"{base_url}/{file_info['path']}"
                local_path = db_path / file_info["path"]
                local_path.parent.mkdir(parents=True, exist_ok=True)
                
                file_size_mb = file_info.get("size", 0) / (1024*1024)
                print(f"  [{idx}/{len(manifest['files'])}] {file_info['path']} ({file_size_mb:.2f}MB)...", end="", flush=True)
                
                urllib.request.urlretrieve(file_url, local_path)
                
                # Verify checksum if provided
                if "checksum" in file_info:
                    if self._verify_checksum(local_path, file_info["checksum"]):
                        print(" ✓")
                    else:
                        print(" ⚠ (checksum mismatch - may be ok)")
                else:
                    print()
            
            print("✓ Vector database downloaded successfully!")
            return True
        
        except Exception as e:
            print(f"Error downloading from manifest: {e}")
            return False
    
    @staticmethod
    def _verify_checksum(file_path: Path, expected_checksum: str) -> bool:
        """Verify file checksum."""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest() == expected_checksum
    
    def load_model(self) -> SentenceTransformer:
        """Load embedding model."""
        if self._model is None:
            print(f"Loading embedding model: {self.config.EMBEDDING_MODEL}...")
            self._model = SentenceTransformer(self.config.EMBEDDING_MODEL)
        return self._model
    
    def get_collection(self):
        """Get or create Chroma collection."""
        if self._collection is None:
            if not self.ensure_db_exists():
                raise FileNotFoundError(
                    f"Vector database not found at {self.config.CHROMA_DB_PATH}. "
                    "Run 'auto-spec setup' to download the database."
                )
            
            self._client = chromadb.PersistentClient(
                path=str(self.config.CHROMA_DB_PATH)
            )
            try:
                self._collection = self._client.get_collection(
                    name=self.config.CHROMA_COLLECTION_NAME
                )
            except Exception as e:
                print(f"Error getting collection: {e}")
                raise
        
        return self._collection
    
    def query(self, text: str, top_k: Optional[int] = None) -> List[Dict]:
        """Query vector database.
        
        Args:
            text: Query text
            top_k: Number of results to return (uses config default if None)
            
        Returns:
            List of similar specifications
        """
        top_k = top_k or self.config.TOP_K_RESULTS
        
        model = self.load_model()
        collection = self.get_collection()
        
        # Embed query
        query_embedding = model.encode([text], convert_to_numpy=False)[0]
        
        # Query collection
        results = collection.query(
            embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"]
        )
        
        # Format results
        formatted_results = []
        if results["documents"] and results["documents"][0]:
            for doc, metadata, distance in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0]
            ):
                # Convert distance to similarity score (0-1)
                similarity = 1 - (distance / 2)
                
                formatted_results.append({
                    "document": doc,
                    "contract_name": metadata.get("contract_name", "Unknown"),
                    "spec_filename": metadata.get("spec_filename", "spec.spec"),
                    "score": similarity,
                    **metadata
                })
        
        return formatted_results
    
    def close(self):
        """Close database connection."""
        if self._client:
            # Chroma doesn't require explicit closing in current versions
            pass
