"""Tests for lint module."""

from auto_spec.lint import lint_spec, LintError


CLEAN_SPEC = """methods {
    function deposit() external;
    function withdraw(uint256) external;
    function totalDeposits() external returns (uint256) envfree;
    function deposits(address) external returns (uint256) envfree;
}

rule depositIncreases(env e) {
    uint256 before = deposits(e.msg.sender);
    deposit(e);
    uint256 after_ = deposits(e.msg.sender);
    assert after_ >= before;
}
"""

SAVINGS_VAULT_SOL = """
pragma solidity ^0.8.20;
contract SavingsVault {
    address public owner;
    uint256 public unlockTime;
    uint256 public totalDeposits;
    mapping(address => uint256) public deposits;
    function deposit() external payable {}
    function withdraw(uint256 amount) external {}
}
"""


class TestKeywordLeak:
    def test_catches_view_in_methods_block(self):
        spec = """methods {
    function balanceOf(address) external view returns (uint256);
}"""
        errors = lint_spec(spec, "")
        assert any(e.category == "keyword_leak" for e in errors)
        assert any("view" in e.message for e in errors)

    def test_catches_payable(self):
        spec = """methods {
    function deposit() external payable;
}"""
        errors = lint_spec(spec, "")
        assert any(e.category == "keyword_leak" for e in errors)

    def test_clean_methods_pass(self):
        spec = """methods {
    function deposit() external;
    function withdraw(uint256) external;
}"""
        errors = lint_spec(spec, "")
        assert not any(e.category == "keyword_leak" for e in errors)


class TestMissingMethods:
    def test_catches_missing_methods_block(self):
        spec = "rule dummy(env e) { assert true; }"
        errors = lint_spec(spec, "")
        assert any(e.category == "missing_methods" for e in errors)


class TestGetterCompleteness:
    def test_catches_referenced_public_var_without_getter(self):
        spec = """methods {
    function deposit() external;
}

rule checkOwner(env e) {
    address o = owner();
    assert o != 0;
}
"""
        contract = "address public owner; function deposit() external {}"
        errors = lint_spec(spec, contract)
        assert any(e.category == "missing_getter" and "owner" in e.message for e in errors)

    def test_passes_when_getter_declared(self):
        spec = """methods {
    function owner() external returns (address) envfree;
}

rule checkOwner(env e) {
    address o = owner();
    assert o != 0;
}
"""
        contract = "address public owner;"
        errors = lint_spec(spec, contract)
        assert not any(e.category == "missing_getter" for e in errors)


class TestForbiddenSum:
    def test_catches_sum_over_non_ghost(self):
        spec = """methods {
    function deposits(address) external returns (uint256);
}

invariant totalIsSum()
    totalDeposits() == sum(deposits);
"""
        errors = lint_spec(spec, "")
        assert any(e.category == "forbidden_sum" for e in errors)

    def test_allows_sum_over_ghost(self):
        spec = """ghost mathint ghostTotal { init_state axiom ghostTotal == 0; }

methods {
    function totalDeposits() external returns (uint256);
}

invariant totalTracked()
    to_mathint(totalDeposits()) == sum(ghostTotal);
"""
        errors = lint_spec(spec, "")
        assert not any(e.category == "forbidden_sum" for e in errors)


class TestInvariantShape:
    def test_catches_old_in_invariant(self):
        spec = """methods {
    function x() external returns (uint256);
}

invariant badInvariant()
    x() >= old(x());
"""
        errors = lint_spec(spec, "")
        assert any(e.category == "invariant_shape" for e in errors)

    def test_clean_invariant_passes(self):
        errors = lint_spec(CLEAN_SPEC, SAVINGS_VAULT_SOL)
        assert not any(e.category == "invariant_shape" for e in errors)


class TestCleanSpec:
    def test_savings_vault_clean_spec_passes_all_checks(self):
        errors = lint_spec(CLEAN_SPEC, SAVINGS_VAULT_SOL)
        assert errors == []


class TestSinvoke:
    def test_catches_sinvoke(self):
        spec = """methods {
    function transfer(address,uint256) external;
}

rule r(env e) {
    sinvoke transfer(e, 0x0, 100);
}
"""
        errors = lint_spec(spec, "")
        assert any(e.category == "deprecated_sinvoke" for e in errors)

    def test_clean_call_passes(self):
        spec = """methods {
    function transfer(address,uint256) external;
}

rule r(env e) {
    transfer(e, 0x0, 100);
}
"""
        errors = lint_spec(spec, "")
        assert not any(e.category == "deprecated_sinvoke" for e in errors)


class TestEnvfreeWithEnv:
    def test_catches_envfree_called_with_env(self):
        spec = """methods {
    function balanceOf(address) external returns (uint256) envfree;
    function transfer(address,uint256) external;
}

rule r(env e) {
    uint256 b = balanceOf(e, e.msg.sender);
}
"""
        errors = lint_spec(spec, "")
        assert any(e.category == "envfree_with_env" for e in errors)

    def test_passes_when_envfree_called_without_env(self):
        spec = """methods {
    function balanceOf(address) external returns (uint256) envfree;
}

rule r(env e) {
    uint256 b = balanceOf(e.msg.sender);
}
"""
        errors = lint_spec(spec, "")
        assert not any(e.category == "envfree_with_env" for e in errors)


class TestRequireParens:
    def test_catches_require_with_parens(self):
        spec = """methods {
    function x() external returns (uint256) envfree;
}

rule r(env e) {
    require(x() > 0);
}
"""
        errors = lint_spec(spec, "")
        assert any(e.category == "require_parens" for e in errors)

    def test_passes_require_without_parens(self):
        spec = """methods {
    function x() external returns (uint256) envfree;
}

rule r(env e) {
    require x() > 0;
}
"""
        errors = lint_spec(spec, "")
        assert not any(e.category == "require_parens" for e in errors)

