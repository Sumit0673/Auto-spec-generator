import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

PROVIDER_DEFAULTS: dict[str, tuple[str, str, str | None]] = {
    "openai":     ("OPENAI_API_KEY",     "gpt-4o-mini",                    None),
    "openrouter": ("OPENROUTER_API_KEY", "openai/gpt-4o-mini",            "https://openrouter.ai/api/v1"),
    "deepseek":   ("DEEPSEEK_API_KEY",   "deepseek-chat",                 "https://api.deepseek.com"),
    "nvidia":     ("NVIDIA_API_KEY",     "meta/llama-3.1-70b-instruct",   "https://integrate.api.nvidia.com/v1"),
    "gemini":     ("GEMINI_API_KEY",     "gemini-2.5-pro",                "__gemini__"),
}


@dataclass
class Config:
    PROJECT_ROOT: Path = Path(__file__).parent.parent

    LLM_PROVIDER: str = "openrouter"
    LLM_MODEL: str = ""       
    LLM_BASE_URL: Optional[str] = None  
    LLM_API_KEY: Optional[str] = None 
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 2000

    CHROMA_DB_PATH: Optional[Path] = None
    CHROMA_DB_REMOTE_URL: Optional[str] = None
    CHROMA_COLLECTION_NAME: str = "erc20_specs"
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_BATCH_SIZE: int = 32

    TOP_K_RESULTS: int = 3
    SIMILARITY_THRESHOLD: float = 0.3
    MIN_RETRIEVAL_SIMILARITY: float = 0.75

    MAX_CONSECUTIVE_SAME_ERRORS: int = 2

    OUTPUT_DIR: Path = Path.cwd() / "auto_spec_output"
    SAVE_SPEC_FILE: bool = True

    ERROR_MEMORY_DB_PATH: Path = Path.home() / ".auto_spec" / "error_memory.db"

    def __post_init__(self):
        load_dotenv()

        self.LLM_PROVIDER = os.getenv("LLM_PROVIDER", self.LLM_PROVIDER).lower()

        if self.LLM_PROVIDER not in PROVIDER_DEFAULTS:
            raise ValueError(
                f"Unknown LLM_PROVIDER={self.LLM_PROVIDER!r}. "
                f"Supported: {', '.join(PROVIDER_DEFAULTS)}"
            )

        env_key, default_model, base_url = PROVIDER_DEFAULTS[self.LLM_PROVIDER]

        self.LLM_API_KEY = os.getenv(env_key)

        self.LLM_MODEL = os.getenv("LLM_MODEL", default_model)

        self.LLM_BASE_URL = base_url if base_url != "__gemini__" else None

        temp_str = os.getenv("LLM_TEMPERATURE")
        if temp_str:
            self.LLM_TEMPERATURE = float(temp_str)

        max_tokens_str = os.getenv("LLM_MAX_TOKENS")

        if max_tokens_str:
            self.LLM_MAX_TOKENS = int(max_tokens_str)

        if self.CHROMA_DB_PATH is None:
            candidates = [
                self.PROJECT_ROOT / "erc20_pairs_final" / "chroma_db",
                self.PROJECT_ROOT / "chroma_db",
                self.PROJECT_ROOT.parent / "chroma_db",
            ]
            for candidate in candidates:
                if candidate.exists():
                    self.CHROMA_DB_PATH = candidate
                    break

            if self.CHROMA_DB_PATH is None:
                self.CHROMA_DB_PATH = self.PROJECT_ROOT / "chroma_db"

        self.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def is_gemini(self) -> bool:
        """Whether the current provider uses the Gemini SDK (not OpenAI-compat)."""
        return self.LLM_PROVIDER == "gemini"

    def validate(self) -> tuple[bool, str]:
        if not self.LLM_API_KEY:
            env_key = PROVIDER_DEFAULTS[self.LLM_PROVIDER][0]
            return False, (
                f"API key not found. Set {env_key} in your .env file "
                f"(provider={self.LLM_PROVIDER})."
            )

        if not self.CHROMA_DB_PATH.exists() and not self.CHROMA_DB_REMOTE_URL:
            return False, (
                f"Chroma DB not found at {self.CHROMA_DB_PATH}. "
                "Run 'auto-spec setup' or set CHROMA_DB_REMOTE_URL."
            )

        return True, ""


def get_config() -> Config:
    if not hasattr(get_config, "_instance"):
        get_config._instance = Config()
    return get_config._instance
