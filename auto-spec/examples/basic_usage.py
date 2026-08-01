#!/usr/bin/env python3
"""
Basic usage example.
"""

from auto_spec import SpecGenerator

# Initialize generator
generator = SpecGenerator()

# Generate spec from contract
spec = generator.generate(
    contract_path="path/to/MyToken.sol",
    query="ERC20 transfer and approval rules",
    top_k=3,
    output_path="output/MyToken.spec"
)

print(spec)
