// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

abstract contract AbstractToken {
    uint256 public totalSupply;

    function transfer(address to, uint256 amount) external virtual returns (bool);

    function _mint(address to, uint256 amount) internal {
        totalSupply += amount;
    }
}
