### SECTION 1: CANDIDATE PROPERTIES OVERVIEW

1. **State Invariants**
	* The total supply of tokens is equal to the sum of all balances.
	* The balance of any address is less than or equal to the total supply.
	* The allowance of any address for any spender is less than or equal to the balance of the address.
2. **Transfer & Arithmetic Rules**
	* The transfer function decreases the sender's balance by the amount transferred and increases the recipient's balance by the amount transferred after deducting the transfer fee.
	* The transfer function increases the fee recipient's balance by the transfer fee.
	* The transferFrom function decreases the sender's balance by the amount transferred and increases the recipient's balance by the amount transferred after deducting the transfer fee.
	* The transferFrom function increases the fee recipient's balance by the transfer fee.
3. **Access Control & Authorization Rules**
	* Only the owner of an address can approve a spender to transfer tokens on their behalf.
	* Only the owner of an address or an approved spender can transfer tokens from that address.
4. **Unitary / Re-entrancy / Solvency Checks**
	* The transfer function does not allow re-entrancy.
	* The transferFrom function does not allow re-entrancy.
	* The contract is solvent, meaning that the total supply of tokens is equal to the sum of all balances.

### SECTION 2: FORMAL CVL SPECIFICATION

```cvl
// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transfer behavior and side effects │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferIntegrity(env e) {
    requireInvariant totalSupplyIsSumOfBalances();
    address holder = e.msg.sender;
    address recipient;
    uint256 amount;
    // cache state
    uint256 holderBalanceBefore = balances[holder];
    uint256 recipientBalanceBefore = balances[recipient];
    // run transaction
    transfer(e, recipient, amount);
    // check outcome
    // balances of holder and recipient are updated
    assert balances[holder] == holderBalanceBefore - (holder == recipient ? 0 : amount);
    assert balances[recipient] == recipientBalanceBefore + (holder == recipient ? 0 : amount);
    // fee recipient's balance is updated
    assert balances[feeRecipient] == balances[feeRecipient] + (amount * transferFee) / 100;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transferFrom behavior and side effects │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferFromIntegrity(env e) {
    requireInvariant totalSupplyIsSumOfBalances();
    address spender = e.msg.sender;
    address holder;
    address recipient;
    uint256 amount;
    // cache state
    uint256 allowanceBefore = allowances[holder][spender];
    uint256 holderBalanceBefore = balances[holder];
    uint256 recipientBalanceBefore = balances[recipient];
    // run transaction
    transferFrom(e, holder, recipient, amount);
    // check outcome
    // allowance is valid & updated
    assert allowanceBefore >= amount;
    assert allowances[holder][spender] == (allowanceBefore == max_uint256 ? max_uint256 : allowanceBefore - amount);
    // balances of holder and recipient are updated
    assert balances[holder] == holderBalanceBefore - (holder == recipient ? 0 : amount);
    assert balances[recipient] == recipientBalanceBefore + (holder == recipient ? 0 : amount);
    // fee recipient's balance is updated
    assert balances[feeRecipient] == balances[feeRecipient] + (amount * transferFee) / 100;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: approve behavior and side effects │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule approveIntegrity(env e) {
    address holder = e.msg.sender;
    address spender;
    uint256 amount;
    approve(e, spender, amount);
    assert allowances[holder][spender] == amount;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: total supply is sum of balances │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
invariant totalSupplyIsSumOfBalances() {
    uint256 sum = 0;
    for (address addr : addresses) {
        sum += balances[addr];
    }
    assert totalSupply == sum;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: balance of any address is less than or equal to total supply │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
invariant balanceLessThanOrEqualTotalSupply(address addr) {
    assert balances[addr] <= totalSupply;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: allowance of any address for any spender is less than or equal to balance of address │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
invariant allowanceLessThanOrEqualBalance(address holder, address spender) {
    assert allowances[holder][spender] <= balances[holder];
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: only owner of address can approve spender to transfer tokens on their behalf │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule onlyOwnerCanApprove(env e) {
    address holder = e.msg.sender;
    address spender;
    uint256 amount;
    approve(e, spender, amount);
    assert holder == e.msg.sender;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: only owner of address or approved spender can transfer tokens from address │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule onlyOwnerOrApprovedSpenderCanTransfer(env e) {
    address holder;
    address recipient;
    uint256 amount;
    transferFrom(e, holder, recipient, amount);
    assert holder == e.msg.sender || allowances[holder][e.msg.sender] >= amount;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transfer function does not allow re-entrancy │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferDoesNotAllowReentrancy(env e) {
    address holder = e.msg.sender;
    address recipient;
    uint256 amount;
    transfer(e, recipient, amount);
    assert !lastReverted;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transferFrom function does not allow re-entrancy │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferFromDoesNotAllowReentrancy(env e) {
    address holder;
    address recipient;
    uint256 amount;
    transferFrom(e, holder, recipient, amount);
    assert !lastReverted;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: contract is solvent, meaning total supply of tokens is equal to sum of all balances │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
invariant contractIsSolvent() {
    uint256 sum = 0;
    for (address addr : addresses) {
        sum += balances[addr];
    }
    assert totalSupply == sum;
}
```