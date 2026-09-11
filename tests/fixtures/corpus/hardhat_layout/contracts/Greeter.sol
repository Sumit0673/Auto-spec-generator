// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Greeter {
    string public greeting;

    function setGreeting(string calldata value) external {
        greeting = value;
    }
}
