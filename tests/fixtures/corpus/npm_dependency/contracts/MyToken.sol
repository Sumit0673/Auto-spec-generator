// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract MyToken {
    uint256 public supply;

    function issue(uint256 amount) external {
        supply += amount;
    }
}
