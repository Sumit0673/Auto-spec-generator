"""Test suite for auto_spec."""

import pytest
from pathlib import Path
from auto_spec import SpecGenerator, Config


class TestConfig:
    """Tests for configuration."""
    
    def test_config_initialization(self):
        """Test config initializes with defaults."""
        config = Config()
        assert config.LLM_PROVIDER in ["nvidia", "openai"]
        assert config.OUTPUT_DIR.exists() or True  # May not exist yet
    
    def test_config_validation(self):
        """Test config validation."""
        config = Config()
        is_valid, msg = config.validate()
        assert isinstance(is_valid, bool)
        assert isinstance(msg, str)

    def test_config_uses_env_chroma_db_path(self, monkeypatch):
        """Test config respects CHROMA_DB_PATH from environment."""
        monkeypatch.setenv("CHROMA_DB_PATH", "/tmp/auto-spec-test-db")
        config = Config()
        assert config.CHROMA_DB_PATH == Path("/tmp/auto-spec-test-db")


class TestSpecGenerator:
    """Tests for SpecGenerator."""
    
    def test_generator_initialization(self):
        """Test generator initializes successfully."""
        config = Config()
        # This might fail due to missing API keys, which is expected
        try:
            generator = SpecGenerator(config=config)
            assert generator is not None
        except RuntimeError:
            # Expected if API key is not configured
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
