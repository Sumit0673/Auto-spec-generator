// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract SavingsVault {
    address public owner;
    uint256 public unlockTime;
    uint256 public totalDeposits;

    mapping(address => uint256) public deposits;

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    constructor(uint256 _unlockTime) {
        owner = msg.sender;
        unlockTime = _unlockTime;
    }

    function deposit() external payable {
        require(msg.value > 0, "Zero deposit");

        deposits[msg.sender] += msg.value;
        totalDeposits += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(block.timestamp >= unlockTime, "Locked");
        require(deposits[msg.sender] >= amount, "Insufficient balance");

        deposits[msg.sender] -= amount;
        totalDeposits -= amount;

        payable(msg.sender).transfer(amount);
    }

    function emergencyWithdraw(address user) external onlyOwner {
        uint256 amount = deposits[user];

        deposits[user] = 0;
        totalDeposits -= amount;

        payable(owner).transfer(amount);
    }

    function extendLock(uint256 newUnlockTime) external onlyOwner {
        require(newUnlockTime > unlockTime, "Invalid time");
        unlockTime = newUnlockTime;
    }

    function balanceOf(address user) external view returns (uint256) {
        return deposits[user];
    }
}
