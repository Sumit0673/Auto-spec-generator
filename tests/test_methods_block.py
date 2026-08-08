"""Tests for methods_block module."""

from pathlib import Path
from auto_spec.methods_block import build_methods_block

FIXTURES = Path(__file__).parent / "fixtures"


class TestMethodsBlockBuilder:
    def test_savings_vault_has_all_public_getters(self):
        source = FIXTURES.joinpath("SavingsVault.sol").read_text()
        block = build_methods_block(source)
        assert "function owner() external returns (address) envfree;" in block
        assert "function unlockTime() external returns (uint256) envfree;" in block
        assert "function totalDeposits() external returns (uint256) envfree;" in block
        assert "function deposits(address) external returns (uint256) envfree;" in block

    def test_savings_vault_has_external_functions(self):
        source = FIXTURES.joinpath("SavingsVault.sol").read_text()
        block = build_methods_block(source)
        assert "function deposit(" in block
        assert "function withdraw(" in block
        assert "function emergencyWithdraw(" in block
        assert "function extendLock(" in block

    def test_simple_token_has_erc20_methods(self):
        source = FIXTURES.joinpath("SimpleToken.sol").read_text()
        block = build_methods_block(source)
        assert "function transfer(" in block
        assert "function approve(" in block
        assert "function transferFrom(" in block
        assert "function totalSupply() external returns (uint256) envfree;" in block
        assert "function balanceOf(address) external returns (uint256) envfree;" in block

    def test_access_control_has_admin_methods(self):
        source = FIXTURES.joinpath("AccessControl.sol").read_text()
        block = build_methods_block(source)
        assert "function grantAdmin(" in block
        assert "function revokeAdmin(" in block
        assert "function transferOwnership(" in block
        assert "function owner() external returns (address) envfree;" in block

    def test_no_solidity_keywords_survive(self):
        source = FIXTURES.joinpath("SavingsVault.sol").read_text()
        block = build_methods_block(source)
        # These should NOT appear in the methods block
        import re
        # Check each line inside methods block
        lines = block.split("\n")
        for line in lines:
            if "function" in line:
                assert not re.search(r'\bview\b', line), f"'view' found in: {line}"
                assert not re.search(r'\bpure\b', line), f"'pure' found in: {line}"
                assert not re.search(r'\bpayable\b', line), f"'payable' found in: {line}"

    def test_no_duplicate_entries(self):
        source = FIXTURES.joinpath("SavingsVault.sol").read_text()
        block = build_methods_block(source)
        # Count function declarations
        import re
        names = re.findall(r'function\s+(\w+)', block)
        assert len(names) == len(set(names)), f"Duplicates found: {names}"

    def test_output_starts_with_methods_block(self):
        source = FIXTURES.joinpath("SavingsVault.sol").read_text()
        block = build_methods_block(source)
        assert block.startswith("methods {")
        assert block.endswith("}")

    def test_nested_mapping_generates_correct_getter(self):
        source = FIXTURES.joinpath("SimpleToken.sol").read_text()
        block = build_methods_block(source)
        # allowance is mapping(address => mapping(address => uint256))
        assert "function allowance(address,address) external returns (uint256) envfree;" in block
