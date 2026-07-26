#!/usr/bin/env python3
"""
Advanced usage with custom configuration.
"""

from pathlib import Path
from auto_spec import SpecGenerator, Config
from auto_spec.vector_db import VectorDBManager

# Create custom configuration
config = Config()
config.LLM_MODEL = "gpt-4"  # Use different model
config.LLM_PROVIDER = "openai"
config.TOP_K_RESULTS = 5  # Get more reference specs
config.OUTPUT_DIR = Path("specs")

# Initialize generator with custom config
generator = SpecGenerator(config=config)

# Generate specification
spec = generator.generate(
    contract_path="path/to/AdvancedToken.sol",
    query="Custom property: check balance conservation",
    top_k=5
)

# Parse the result
from auto_spec.utils import parse_property_sections

parsed = parse_property_sections(spec)
print("Overview:")
print(parsed["overview"])
print("\n" + "="*80 + "\n")
print("Specification:")
print(parsed["specification"])
