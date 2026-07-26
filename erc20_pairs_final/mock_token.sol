// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

/**
 * @title MockToken
 * @dev A simple ERC20-like token for testing the RAG specification generator.
 * It features a basic transfer fee to ensure the LLM generates rules handling fees.
 */
contract MockToken {
    mapping(address => uint256) public balances;
    mapping(address => mapping(address => uint256)) public allowances;
    uint256 public totalSupply;
    
    // Fee percentage (1 = 1%)
    uint256 public transferFee = 1;
    address public feeRecipient;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(uint256 _initialSupply, address _feeRecipient) {
        totalSupply = _initialSupply;
        balances[msg.sender] = _initialSupply;
        feeRecipient = _feeRecipient;
    }

    function transfer(address to, uint256 amount) public returns (bool) {
        require(balances[msg.sender] >= amount, "Insufficient balance");
        
        uint256 fee = (amount * transferFee) / 100;
        uint256 amountAfterFee = amount - fee;

        balances[msg.sender] -= amount;
        balances[to] += amountAfterFee;
        balances[feeRecipient] += fee;

        emit Transfer(msg.sender, to, amountAfterFee);
        emit Transfer(msg.sender, feeRecipient, fee);
        
        return true;
    }

    function approve(address spender, uint256 amount) public returns (bool) {
        allowances[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) public returns (bool) {
        require(balances[from] >= amount, "Insufficient balance");
        require(allowances[from][msg.sender] >= amount, "Insufficient allowance");

        uint256 fee = (amount * transferFee) / 100;
        uint256 amountAfterFee = amount - fee;

        allowances[from][msg.sender] -= amount;
        balances[from] -= amount;
        balances[to] += amountAfterFee;
        balances[feeRecipient] += fee;

        emit Transfer(from, to, amountAfterFee);
        emit Transfer(from, feeRecipient, fee);

        return true;
    }
}
