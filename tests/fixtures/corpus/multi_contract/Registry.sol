// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Store {
    mapping(address => uint256) public balances;

    function set(address who, uint256 amount) external {
        balances[who] = amount;
    }
}

contract Registry {
    Store public store;
    uint256 public entries;

    function record() external {
        entries += 1;
    }
}
