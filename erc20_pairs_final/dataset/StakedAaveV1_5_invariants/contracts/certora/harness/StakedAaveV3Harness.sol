pragma solidity ^0.8.0;

import {StakedAaveV3} from '../munged/contracts/StakedAaveV3.sol';
import {IERC20} from '../munged/interfaces/IERC20.sol';

contract StakedAaveV3Harness is StakedAaveV3 {
  constructor(
    IERC20 stakedToken,
    IERC20 rewardToken,
    uint256 unstakeWindow,
    address rewardsVault,
    address emissionManager,
    uint128 distributionDuration
  )
    StakedAaveV3(
      stakedToken,
      rewardToken,
      unstakeWindow,
      rewardsVault,
      emissionManager,
      distributionDuration
    )
  {}
  function cooldownAmount(address user) public view returns (uint216) {
    return stakersCooldowns[user].amount;
  }
  function cooldownTimestamp(address user) public view returns (uint40) {
    return stakersCooldowns[user].timestamp;
  }
  function getAssetEmissionPerSecond(address token)
    public
    view
    returns (uint128)
  {
    return assets[token].emissionPerSecond;
  }
  function getAssetLastUpdateTimestamp(address token)
    public
    view
    returns (uint128)
  {
    return assets[token].lastUpdateTimestamp;
  }
  function getAssetGlobalIndex(address token) public view returns (uint256) {
    return assets[token].index;
  }
  function getUserPersonalIndex(address token, address user)
    public
    view
    returns (uint256)
  {
    return assets[token].users[user];
  }

  function _getExchangeRateWrapper(uint256 totalAssets, uint256 totalShares)
    public
    pure
    returns (uint216)
  {
    return _getExchangeRate(totalAssets, totalShares);
  }
}