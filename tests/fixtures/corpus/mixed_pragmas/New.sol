// SPDX-License-Identifier: MIT
pragma solidity 0.8.19;

contract New {
    uint256 public value;

    function setValue(uint256 v) external {
        value = v;
    }
}
