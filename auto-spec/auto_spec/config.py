"""
Configuration management for Auto-Spec.

Handles environment variables, API keys, model selection, and database paths.
"""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    """Configuration for Auto-Spec tool."""
    
    # Project root
    PROJECT_ROOT: Path = Path(__file__).parent.parent
    
    # LLM Configuration
    LLM_PROVIDER: str = "nvidia"  # or "openai", "anthropic", etc.
    LLM_MODEL: str = "meta/llama-3.1-70b-instruct"
    LLM_BASE_URL: str = "https://integrate.api.nvidia.com/v1"
    LLM_API_KEY: Optional[str] = None
    LLM_TEMPERATURE: float = 0.2
    
    # Vector Database Configuration
    CHROMA_DB_PATH: Optional[Path] = None
    CHROMA_DB_REMOTE_URL: Optional[str] = None  # For downloading pre-built DB
    CHROMA_COLLECTION_NAME: str = "erc20_specs"
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_BATCH_SIZE: int = 32
    
    # RAG Configuration
    TOP_K_RESULTS: int = 3
    SIMILARITY_THRESHOLD: float = 0.3
    
    # Output Configuration
    OUTPUT_DIR: Path = Path.cwd() / "auto_spec_output"
    SAVE_SPEC_FILE: bool = True
    
    def __post_init__(self):
        """Initialize configuration from environment variables."""
        load_dotenv()
        
        # Override from environment variables
        self.LLM_MODEL = os.getenv("LLM_MODEL", self.LLM_MODEL)
        self.LLM_API_KEY = os.getenv("NVIDIA_API_KEY") or os.getenv("OPENAI_API_KEY")
        
        # Set default Chroma DB path if not specified
        if self.CHROMA_DB_PATH is None:
            self.CHROMA_DB_PATH = self.PROJECT_ROOT.parent / "chroma_db"
        
        # Create output directory
        self.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    def validate(self) -> tuple[bool, str]:
        """Validate configuration.
        
        Returns:
            tuple: (is_valid, error_message)
        """
        if not self.LLM_API_KEY:
            return False, "LLM API Key not found. Set NVIDIA_API_KEY or OPENAI_API_KEY environment variable."
        
        if not self.CHROMA_DB_PATH.exists() and not self.CHROMA_DB_REMOTE_URL:
            return False, (
                f"Chroma DB not found at {self.CHROMA_DB_PATH}. "
                "Run 'auto-spec setup' or set CHROMA_DB_REMOTE_URL."
            )
        
        return True, ""


def get_config() -> Config:
    """Get or create global config instance."""
    if not hasattr(get_config, "_instance"):
        get_config._instance = Config()
    return get_config._instance
