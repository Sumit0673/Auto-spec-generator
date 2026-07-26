pragma solidity ^0.8.0;

import {AaveTokenV3} from '../munged/src/AaveTokenV3.sol';
import {DelegationMode} from '../munged/src/DelegationAwareBalance.sol';

contract AaveTokenV3Harness is AaveTokenV3 {
  function getBalance(address user) public view returns (uint104) {
    return _balances[user].balance;
  }
  function getDelegatedPropositionBalance(address user) public view returns (uint72) {
    return _balances[user].delegatedPropositionBalance;
  }
  function getDelegatedVotingBalance(address user) public view returns (uint72) {
    return _balances[user].delegatedVotingBalance;
  }
  function getDelegatingProposition(address user) public view returns (bool) {
    return
      _balances[user].delegationMode == DelegationMode.PROPOSITION_DELEGATED ||
      _balances[user].delegationMode == DelegationMode.FULL_POWER_DELEGATED;
  }
  function getDelegatingVoting(address user) public view returns (bool) {
    return
      _balances[user].delegationMode == DelegationMode.VOTING_DELEGATED ||
      _balances[user].delegationMode == DelegationMode.FULL_POWER_DELEGATED;
  }
  function getVotingDelegatee(address user) public view returns (address) {
      return _votingDelegatee[user];
  }
  function getPropositionDelegatee(address user) public view returns (address) {
    return _propositionDelegatee[user];
  }
  function getDelegationMode(address user) public view returns (DelegationMode) {
    return _balances[user].delegationMode;
  }

  function getDelegatedPowerVoting(address user) public view returns (uint256) {
      DelegationState memory userState = _getDelegationState(user);
      uint256 userDelegatedPower = _getDelegatedPowerByType(userState, GovernancePowerType.VOTING);
      
      return userDelegatedPower;
  }
}