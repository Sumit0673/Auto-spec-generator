// SPDX-License-Identifier: MIT
pragma solidity 0.6.12;

contract Old {
    uint256 public value;

    function setValue(uint256 v) external {
        value = v;
    }
}
