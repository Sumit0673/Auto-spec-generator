"""
Auto-Spec: Automated CVL Specification Generation for Smart Contracts
======================================================================

A production-ready tool for generating Certora Verification Language (CVL)
specifications from Solidity smart contracts using RAG (Retrieval-Augmented Generation)
and LLM in-context learning.

Quick Start:
    from auto_spec import SpecGenerator
    
    generator = SpecGenerator()
    spec = generator.generate(
        contract_path="path/to/MyToken.sol",
        query="ERC20 transfer rules"
    )
"""

__version__ = "2.0.0"
__author__ = "Auto-Spec Contributors"

from auto_spec.generator import SpecGenerator
from auto_spec.config import Config

__all__ = ["SpecGenerator", "Config", "__version__"]
