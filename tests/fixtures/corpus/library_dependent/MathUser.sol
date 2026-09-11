// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

library SafeAdd {
    function add(uint256 a, uint256 b) internal pure returns (uint256) {
        uint256 c = a + b;
        require(c >= a, "overflow");
        return c;
    }
}

contract MathUser {
    using SafeAdd for uint256;

    uint256 public total;

    function bump(uint256 amount) external {
        total = total.add(amount);
    }
}
