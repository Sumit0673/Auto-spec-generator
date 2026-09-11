// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Base {
    uint256 public level;

    function base() external {
        level = 1;
    }
}

contract Middle is Base {
    function middle() external {
        level = 2;
    }
}

contract Upper is Middle {
    function upper() external {
        level = 3;
    }
}

contract Leaf is Upper {
    function leaf() external {
        level = 4;
    }
}
