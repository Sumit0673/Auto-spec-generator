// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract AccessControl {
    address public owner;
    mapping(address => bool) public admins;
    uint256 public threshold;
    bool public paused;

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    modifier onlyAdmin() {
        require(admins[msg.sender] || msg.sender == owner, "Not admin");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "Paused");
        _;
    }

    constructor(uint256 _threshold) {
        owner = msg.sender;
        threshold = _threshold;
    }

    function grantAdmin(address account) external onlyOwner {
        admins[account] = true;
    }

    function revokeAdmin(address account) external onlyOwner {
        admins[account] = false;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Zero address");
        owner = newOwner;
    }

    function setThreshold(uint256 newThreshold) external onlyAdmin whenNotPaused {
        threshold = newThreshold;
    }

    function pause() external onlyOwner {
        paused = true;
    }

    function unpause() external onlyOwner {
        paused = false;
    }
}
