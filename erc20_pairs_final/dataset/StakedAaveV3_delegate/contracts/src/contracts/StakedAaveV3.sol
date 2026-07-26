pragma solidity ^0.8.0;

import {IERC20} from 'openzeppelin-contracts/contracts/token/ERC20/IERC20.sol';
import {DistributionTypes} from '../lib/DistributionTypes.sol';
import {StakedTokenV3} from './StakedTokenV3.sol';
import {IGhoVariableDebtTokenTransferHook} from '../interfaces/IGhoVariableDebtTokenTransferHook.sol';
import {SafeCast} from '../lib/SafeCast.sol';
import {IStakedAaveV3} from '../interfaces/IStakedAaveV3.sol';

contract StakedAaveV3 is StakedTokenV3, IStakedAaveV3 {
  using SafeCast for uint256;

  uint256[1] private ______DEPRECATED_FROM_STK_AAVE_V3;
  IGhoVariableDebtTokenTransferHook public ghoDebtToken;

  function REVISION() public pure virtual override returns (uint256) {
    return 6;
  }

  constructor(
    IERC20 stakedToken,
    IERC20 rewardToken,
    uint256 unstakeWindow,
    address rewardsVault,
    address emissionManager,
    uint128 distributionDuration
  )
    StakedTokenV3(
      stakedToken,
      rewardToken,
      unstakeWindow,
      rewardsVault,
      emissionManager,
      distributionDuration
    )
  {
    lastInitializedRevision = REVISION();
  }

  
  function initialize() external override initializer {}
  function claimRewardsAndStake(
    address to,
    uint256 amount
  ) external override returns (uint256) {
    return _claimRewardsAndStakeOnBehalf(msg.sender, to, amount);
  }
  function claimRewardsAndStakeOnBehalf(
    address from,
    address to,
    uint256 amount
  ) external override onlyClaimHelper returns (uint256) {
    return _claimRewardsAndStakeOnBehalf(from, to, amount);
  }

  
  function _afterTokenTransfer(
    address from,
    address to,
    uint256 fromBalanceBefore,
    uint256 toBalanceBefore,
    uint256 amount
  ) internal override {
    super._afterTokenTransfer(
      from,
      to,
      fromBalanceBefore,
      toBalanceBefore,
      amount
    );

    address cachedGhoDebtToken = address(ghoDebtToken);
    if (cachedGhoDebtToken != address(0)) {
      _updateDiscountDistribution(
        cachedGhoDebtToken,
        from,
        to,
        fromBalanceBefore,
        toBalanceBefore,
        amount
      );
    }
  }
  function _updateDiscountDistribution(
    address cachedGhoDebtToken,
    address from,
    address to,
    uint256 fromBalanceBefore,
    uint256 toBalanceBefore,
    uint256 amount
  ) internal {
    bytes4 selector = IGhoVariableDebtTokenTransferHook
      .updateDiscountDistribution
      .selector;
    uint256 gasLimit = 220_000;
    assembly {
      let ptr := mload(0x40)
      mstore(ptr, selector)
      mstore(add(ptr, 0x04), from)
      mstore(add(ptr, 0x24), to)
      mstore(add(ptr, 0x44), fromBalanceBefore)
      mstore(add(ptr, 0x64), toBalanceBefore)
      mstore(add(ptr, 0x84), amount)

      let gasLeft := gas()
      if iszero(call(gasLimit, cachedGhoDebtToken, 0, ptr, 0xA4, 0, 0)) {
        if lt(div(mul(gasLeft, 63), 64), gasLimit) {
          returndatacopy(ptr, 0, returndatasize())
          revert(ptr, returndatasize())
        }
      }
    }
  }
}