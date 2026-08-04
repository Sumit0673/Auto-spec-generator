"""Shared, lightweight representations for contract-to-CVL retrieval."""

from __future__ import annotations

import re

INDEX_SEPARATOR = "\n\nCVL SPECIFICATION:\n"


def contract_profile(contract_code: str, contract_name: str = "TargetContract") -> str:
    """Describe a Solidity contract compactly enough for the embedding model."""
    functions = re.findall(
        r"\bfunction\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", contract_code, re.S
    )
    signatures = [name + "(" + re.sub(r"\s+", " ", args).strip() + ")" for name, args in functions]
    lowered = contract_code.lower()
    features = [
        name for name, markers in {
            "erc20": ("transfer", "transferfrom", "balanceof", "allowance"),
            "mint-burn": ("mint", "burn"),
            "permit": ("permit", "nonces", "domain_separator"),
            "access-control": ("onlyowner", "onlyrole", "owner", "admin"),
            "pausable": ("pause", "unpause", "whennotpaused"),
            "fees": ("fee", "tax", "treasury"),
            "delegation": ("delegate", "votes", "checkpoint"),
            "upgradeable": ("upgrade", "initializer", "proxy"),
        }.items()
        if any(marker in lowered for marker in markers)
    ]
    return "\n".join([
        f"SOLIDITY CONTRACT: {contract_name}",
        f"FEATURES: {', '.join(features) or 'none detected'}",
        f"FUNCTIONS: {', '.join(signatures[:40]) or 'none detected'}",
    ])


def make_index_document(profile: str, spec_chunk: str) -> str:
    """Store contract evidence for retrieval while retaining the usable CVL chunk."""
    return f"{profile}{INDEX_SEPARATOR}{spec_chunk.strip()}"


def extract_spec_chunk(index_document: str) -> str:
    """Return only CVL to the generation prompt; never leak index scaffolding."""
    return index_document.partition(INDEX_SEPARATOR)[2].strip() or index_document.strip()
