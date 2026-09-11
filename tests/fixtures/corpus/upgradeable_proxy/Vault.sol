// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Vault {
    bool private initialized;
    address public owner;
    uint256 public deposits;

    modifier initializer() {
        require(!initialized, "already initialized");
        initialized = true;
        _;
    }

    function initialize(address newOwner) external initializer {
        owner = newOwner;
    }

    function deposit(uint256 amount) external {
        deposits += amount;
    }
}
