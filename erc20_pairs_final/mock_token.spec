// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: totalSupply is sum of all balances │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule totalSupplyIsSumOfBalances() {
  mathint sum = 0;
  for (address account) {
    sum += balanceOf(account);
  }
  assert sum == totalSupply();
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transfer behavior and side effects │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferIntegrity(env e) {
  requireInvariant totalSupplyIsSumOfBalances();
  address holder = e.msg.sender;
  address recipient;
  uint256 amount;
  // cache state
  uint256 holderBalanceBefore = balanceOf(holder);
  uint256 recipientBalanceBefore = balanceOf(recipient);
  uint256 feeRecipientBalanceBefore = balanceOf(feeRecipient);
  // run transaction
  transfer(e, recipient, amount);
  // check outcome
  // balances of holder and recipient are updated
  uint256 fee = (amount * transferFee) / 100;
  uint256 amountAfterFee = amount - fee;
  assert balanceOf(holder) == holderBalanceBefore - amount;
  assert balanceOf(recipient) == recipientBalanceBefore + amountAfterFee;
  assert balanceOf(feeRecipient) == feeRecipientBalanceBefore + fee;
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
  uint256 allowanceBefore = allowance(holder, spender);
  uint256 holderBalanceBefore = balanceOf(holder);
  uint256 recipientBalanceBefore = balanceOf(recipient);
  uint256 feeRecipientBalanceBefore = balanceOf(feeRecipient);
  // run transaction
  transferFrom(e, holder, recipient, amount);
  // allowance is valid & updated
  assert allowanceBefore >= amount;
  assert allowance(holder, spender) == (allowanceBefore == max_uint256 ? max_uint256 : allowanceBefore - amount);
  // balances of holder and recipient are updated
  uint256 fee = (amount * transferFee) / 100;
  uint256 amountAfterFee = amount - fee;
  assert balanceOf(holder) == holderBalanceBefore - amount;
  assert balanceOf(recipient) == recipientBalanceBefore + amountAfterFee;
  assert balanceOf(feeRecipient) == feeRecipientBalanceBefore + fee;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: approve behavior and side effects │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule approveIntegrity(env e) {
  address holder = e.msg.sender;
  address spender;
  uint256 amount;
  approve(e, spender, amount);
  assert allowance(holder, spender) == amount;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transfer does not affect third party │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferDoesNotAffectThirdParty(env e) {
  address addr1;
  uint256 amount;
  address addr2;
  require addr1 != addr2 && addr2 != e.msg.sender;
  uint256 before = balanceOf(addr2);
  transfer(e, addr1, amount);
  assert balanceOf(addr2) == before;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transferFrom does not affect third party │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferFromDoesNotAffectThirdParty(env e) {
  address spender = e.msg.sender;
  address owner;
  address recepient;
  address thirdParty;
  address everyUser;
  require thirdParty != owner && thirdParty != recepient && thirdParty != spender;
  uint256 thirdPartyBalanceBefore = balanceOf(thirdParty);
  uint256 thirdPartyAllowanceBefore = allowance(thirdParty, everyUser);
  uint256 transfered;
  transferFrom(e, owner, recepient, transfered);
  uint256 thirdPartyBalanceAfter = balanceOf(thirdParty);
  uint256 thirdPartyAllowanceAfter = allowance(thirdParty, everyUser);
  assert thirdPartyBalanceBefore == thirdPartyBalanceAfter;
  assert thirdPartyAllowanceBefore == thirdPartyAllowanceAfter;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: approve does not affect third party │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule approveDoesNotAffectThirdParty(env e) {
  address spender;
  address owner = e.msg.sender;
  address thirdParty;
  address everyUser;
  require thirdParty != owner && thirdParty != spender;
  uint amount;
  uint256 thirdPartyAllowanceBefore = allowance(thirdParty, everyUser);
  approve(e, spender, amount);
  uint256 thirdPartyAllowanceAfter = allowance(thirdParty, everyUser);
  assert thirdPartyAllowanceBefore == thirdPartyAllowanceBefore;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transfer reverts when insufficient balance │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferRevertsWhenInsufficientBalance(env e) {
  address recipient;
  uint256 amount;
  require balanceOf(e.msg.sender) < amount;
  transfer@withrevert(e, recipient, amount);
  assert lastReverted;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transferFrom reverts when insufficient balance │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferFromRevertsWhenInsufficientBalance(env e) {
  address owner;
  address spender = e.msg.sender;
  address recepient;
  uint256 allowed = allowance(owner, spender);
  uint256 transfered;
  require balanceOf(owner) < transfered;
  transferFrom@withrevert(e, owner, recepient, transfered);
  assert lastReverted;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transferFrom reverts when insufficient allowance │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferFromRevertsWhenInsufficientAllowance(env e) {
  address owner;
  address spender = e.msg.sender;
  address recepient;
  uint256 allowed = allowance(owner, spender);
  uint256 transfered;
  require allowed < transfered;
  transferFrom@withrevert(e, owner, recepient, transfered);
  assert lastReverted;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transfer fee is calculated correctly │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferFeeIsCalculatedCorrectly(env e) {
  address recipient;
  uint256 amount;
  uint256 fee = (amount * transferFee) / 100;
  transfer(e, recipient, amount);
  assert balanceOf(feeRecipient) == fee;
}

// ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
// │ Rule: transferFrom fee is calculated correctly │
// └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
rule transferFromFeeIsCalculatedCorrectly(env e) {
  address owner;
  address spender = e.msg.sender;
  address recepient;
  uint256 amount;
  uint256 fee = (amount * transferFee) / 100;
  transferFrom(e, owner, recepient, amount);
  assert balanceOf(feeRecipient) == fee;
}