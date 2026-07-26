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
            return True
        
        if self.config.CHROMA_DB_REMOTE_URL:
            print(f"Downloading vector database from {self.config.CHROMA_DB_REMOTE_URL}...")
            return self.download_db()
        
        return False
    
    def download_db(self) -> bool:
        """Download pre-built vector database.
        
        Returns:
            bool: Success status
        """
        if not self.config.CHROMA_DB_REMOTE_URL:
            print("Error: CHROMA_DB_REMOTE_URL not configured")
            return False
        
        try:
            db_path = self.config.CHROMA_DB_PATH
            db_path.mkdir(parents=True, exist_ok=True)
            
            # Download manifest
            manifest_url = urljoin(self.config.CHROMA_DB_REMOTE_URL, "manifest.json")
            manifest_path = db_path / "manifest.json"
            
            print(f"Downloading from {manifest_url}...")
            urllib.request.urlretrieve(manifest_url, manifest_path)
            
            with open(manifest_path) as f:
                manifest = json.load(f)
            
            # Download database files
            for file_info in manifest.get("files", []):
                file_url = urljoin(self.config.CHROMA_DB_REMOTE_URL, file_info["path"])
                local_path = db_path / file_info["path"]
                local_path.parent.mkdir(parents=True, exist_ok=True)
                
                print(f"  Downloading {file_info['path']}...")
                urllib.request.urlretrieve(file_url, local_path)
                
                # Verify checksum if provided
                if "checksum" in file_info:
                    if not self._verify_checksum(local_path, file_info["checksum"]):
                        print(f"  Warning: Checksum mismatch for {file_info['path']}")
            
            print("Vector database downloaded successfully!")
            return True
        
        except Exception as e:
            print(f"Error downloading vector database: {e}")
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
