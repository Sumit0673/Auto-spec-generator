pragma solidity ^0.8.10;

import {IERC20} from '@aave/core-v3/contracts/dependencies/openzeppelin/contracts/IERC20.sol';
import {SafeCast} from '@aave/core-v3/contracts/dependencies/openzeppelin/contracts/SafeCast.sol';
import {VersionedInitializable} from '@aave/core-v3/contracts/protocol/libraries/aave-upgradeability/VersionedInitializable.sol';
import {WadRayMath} from '@aave/core-v3/contracts/protocol/libraries/math/WadRayMath.sol';
import {PercentageMath} from '@aave/core-v3/contracts/protocol/libraries/math/PercentageMath.sol';
import {Errors} from '@aave/core-v3/contracts/protocol/libraries/helpers/Errors.sol';
import {IPool} from '@aave/core-v3/contracts/interfaces/IPool.sol';
import {IAaveIncentivesController} from '@aave/core-v3/contracts/interfaces/IAaveIncentivesController.sol';
import {IInitializableDebtToken} from '@aave/core-v3/contracts/interfaces/IInitializableDebtToken.sol';
import {IVariableDebtToken} from '@aave/core-v3/contracts/interfaces/IVariableDebtToken.sol';
import {EIP712Base} from '@aave/core-v3/contracts/protocol/tokenization/base/EIP712Base.sol';
import {DebtTokenBase} from '@aave/core-v3/contracts/protocol/tokenization/base/DebtTokenBase.sol';
import {IGhoDiscountRateStrategy} from '../interestStrategy/interfaces/IGhoDiscountRateStrategy.sol';
import {IGhoVariableDebtToken} from './interfaces/IGhoVariableDebtToken.sol';
import {ScaledBalanceTokenBase} from './base/ScaledBalanceTokenBase.sol';

contract GhoVariableDebtToken is DebtTokenBase, ScaledBalanceTokenBase, IGhoVariableDebtToken {
  using WadRayMath for uint256;
  using SafeCast for uint256;
  using PercentageMath for uint256;

  uint256 public constant DEBT_TOKEN_REVISION = 0x1;
  address internal _ghoAToken;
  IERC20 internal _discountToken;
  IGhoDiscountRateStrategy internal _discountRateStrategy;

  struct GhoUserState {
    uint128 accumulatedDebtInterest;
    uint16 discountPercent;
  }
  mapping(address => GhoUserState) internal _ghoUserState;

  
  modifier onlyDiscountToken() {
    require(address(_discountToken) == msg.sender, 'CALLER_NOT_DISCOUNT_TOKEN');
    _;
  }

  
  modifier onlyAToken() {
    require(_ghoAToken == msg.sender, 'CALLER_NOT_A_TOKEN');
    _;
  }

  
  constructor(
    IPool pool
  )
    DebtTokenBase()
    ScaledBalanceTokenBase(pool, 'GHO_VARIABLE_DEBT_TOKEN_IMPL', 'GHO_VARIABLE_DEBT_TOKEN_IMPL', 0)
  {
  }
  function initialize(
    IPool initializingPool,
    address underlyingAsset,
    IAaveIncentivesController incentivesController,
    uint8 debtTokenDecimals,
    string memory debtTokenName,
    string memory debtTokenSymbol,
    bytes calldata params
  ) external override initializer {
    require(initializingPool == POOL, Errors.POOL_ADDRESSES_DO_NOT_MATCH);
    _setName(debtTokenName);
    _setSymbol(debtTokenSymbol);
    _setDecimals(debtTokenDecimals);

    _underlyingAsset = underlyingAsset;
    _incentivesController = incentivesController;

    _domainSeparator = _calculateDomainSeparator();

    emit Initialized(
      underlyingAsset,
      address(POOL),
      address(incentivesController),
      debtTokenDecimals,
      debtTokenName,
      debtTokenSymbol,
      params
    );
  }
  function getRevision() internal pure virtual override returns (uint256) {
    return DEBT_TOKEN_REVISION;
  }
  function balanceOf(address user) public view virtual override returns (uint256) {
    uint256 scaledBalance = super.balanceOf(user);

    if (scaledBalance == 0) {
      return 0;
    }

    uint256 index = POOL.getReserveNormalizedVariableDebt(_underlyingAsset);
    uint256 previousIndex = _userState[user].additionalData;
    uint256 balance = scaledBalance.rayMul(index);
    if (index == previousIndex) {
      return balance;
    }

    uint256 discountPercent = _ghoUserState[user].discountPercent;
    if (discountPercent != 0) {
      uint256 balanceIncrease = balance - scaledBalance.rayMul(previousIndex);
      balance -= balanceIncrease.percentMul(discountPercent);
    }

    return balance;
  }
  function mint(
    address user,
    address onBehalfOf,
    uint256 amount,
    uint256 index
  ) external virtual override onlyPool returns (bool, uint256) {
    if (user != onBehalfOf) {
      _decreaseBorrowAllowance(onBehalfOf, user, amount);
    }
    return (_mintScaled(user, onBehalfOf, amount, index), scaledTotalSupply());
  }
  function burn(
    address from,
    uint256 amount,
    uint256 index
  ) external virtual override onlyPool returns (uint256) {
    _burnScaled(from, address(0), amount, index);
    return scaledTotalSupply();
  }

  
  function totalSupply() public view virtual override returns (uint256) {
    return super.totalSupply().rayMul(POOL.getReserveNormalizedVariableDebt(_underlyingAsset));
  }
  function _EIP712BaseId() internal view override returns (string memory) {
    return name();
  }

  
  function transfer(address, uint256) external virtual override returns (bool) {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }

  function allowance(address, address) external view virtual override returns (uint256) {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }

  function approve(address, uint256) external virtual override returns (bool) {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }

  function transferFrom(address, address, uint256) external virtual override returns (bool) {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }

  function increaseAllowance(address, uint256) external virtual override returns (bool) {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }

  function decreaseAllowance(address, uint256) external virtual override returns (bool) {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }
  function UNDERLYING_ASSET_ADDRESS() external view override returns (address) {
    return _underlyingAsset;
  }
  function setAToken(address ghoAToken) external override onlyPoolAdmin {
    require(_ghoAToken == address(0), 'ATOKEN_ALREADY_SET');
    require(ghoAToken != address(0), 'ZERO_ADDRESS_NOT_VALID');
    _ghoAToken = ghoAToken;
    emit ATokenSet(ghoAToken);
  }
  function getAToken() external view override returns (address) {
    return _ghoAToken;
  }
  function updateDiscountRateStrategy(
    address newDiscountRateStrategy
  ) external override onlyPoolAdmin {
    require(newDiscountRateStrategy != address(0), 'ZERO_ADDRESS_NOT_VALID');
    address oldDiscountRateStrategy = address(_discountRateStrategy);
    _discountRateStrategy = IGhoDiscountRateStrategy(newDiscountRateStrategy);
    emit DiscountRateStrategyUpdated(oldDiscountRateStrategy, newDiscountRateStrategy);
  }
  function getDiscountRateStrategy() external view override returns (address) {
    return address(_discountRateStrategy);
  }
  function updateDiscountToken(address newDiscountToken) external override onlyPoolAdmin {
    require(newDiscountToken != address(0), 'ZERO_ADDRESS_NOT_VALID');
    address oldDiscountToken = address(_discountToken);
    _discountToken = IERC20(newDiscountToken);
    emit DiscountTokenUpdated(oldDiscountToken, newDiscountToken);
  }
  function getDiscountToken() external view override returns (address) {
    return address(_discountToken);
  }
  function updateDiscountDistribution(
    address sender,
    address recipient,
    uint256 senderDiscountTokenBalance,
    uint256 recipientDiscountTokenBalance,
    uint256 amount
  ) external override onlyDiscountToken {
    if (sender == recipient) {
      return;
    }

    uint256 senderPreviousScaledBalance = super.balanceOf(sender);
    uint256 recipientPreviousScaledBalance = super.balanceOf(recipient);
    if (senderPreviousScaledBalance == 0 && recipientPreviousScaledBalance == 0) {
      return;
    }

    uint256 index = POOL.getReserveNormalizedVariableDebt(_underlyingAsset);

    uint256 balanceIncrease;
    uint256 discountScaled;

    if (senderPreviousScaledBalance > 0) {
      (balanceIncrease, discountScaled) = _accrueDebtOnAction(
        sender,
        senderPreviousScaledBalance,
        _ghoUserState[sender].discountPercent,
        index
      );

      _burn(sender, discountScaled.toUint128());

      _refreshDiscountPercent(
        sender,
        super.balanceOf(sender).rayMul(index),
        senderDiscountTokenBalance - amount,
        _ghoUserState[sender].discountPercent
      );

      emit Transfer(address(0), sender, balanceIncrease);
      emit Mint(address(0), sender, balanceIncrease, balanceIncrease, index);
    }

    if (recipientPreviousScaledBalance > 0) {
      (balanceIncrease, discountScaled) = _accrueDebtOnAction(
        recipient,
        recipientPreviousScaledBalance,
        _ghoUserState[recipient].discountPercent,
        index
      );

      _burn(recipient, discountScaled.toUint128());

      _refreshDiscountPercent(
        recipient,
        super.balanceOf(recipient).rayMul(index),
        recipientDiscountTokenBalance + amount,
        _ghoUserState[recipient].discountPercent
      );

      emit Transfer(address(0), recipient, balanceIncrease);
      emit Mint(address(0), recipient, balanceIncrease, balanceIncrease, index);
    }
  }
  function getDiscountPercent(address user) external view override returns (uint256) {
    return _ghoUserState[user].discountPercent;
  }
  function getBalanceFromInterest(address user) external view override returns (uint256) {
    return _ghoUserState[user].accumulatedDebtInterest;
  }
  function decreaseBalanceFromInterest(address user, uint256 amount) external override onlyAToken {
    _ghoUserState[user].accumulatedDebtInterest = (_ghoUserState[user].accumulatedDebtInterest -
      amount).toUint128();
  }
  function rebalanceUserDiscountPercent(address user) external override {
    uint256 index = POOL.getReserveNormalizedVariableDebt(_underlyingAsset);
    uint256 previousScaledBalance = super.balanceOf(user);
    uint256 discountPercent = _ghoUserState[user].discountPercent;

    (uint256 balanceIncrease, uint256 discountScaled) = _accrueDebtOnAction(
      user,
      previousScaledBalance,
      discountPercent,
      index
    );

    _burn(user, discountScaled.toUint128());

    _refreshDiscountPercent(
      user,
      super.balanceOf(user).rayMul(index),
      _discountToken.balanceOf(user),
      discountPercent
    );

    emit Transfer(address(0), user, balanceIncrease);
    emit Mint(address(0), user, balanceIncrease, balanceIncrease, index);
  }

  
  function _mintScaled(
    address caller,
    address onBehalfOf,
    uint256 amount,
    uint256 index
  ) internal override returns (bool) {
    uint256 amountScaled = amount.rayDiv(index);
    require(amountScaled != 0, Errors.INVALID_MINT_AMOUNT);

    uint256 previousScaledBalance = super.balanceOf(onBehalfOf);
    uint256 discountPercent = _ghoUserState[onBehalfOf].discountPercent;
    (uint256 balanceIncrease, uint256 discountScaled) = _accrueDebtOnAction(
      onBehalfOf,
      previousScaledBalance,
      discountPercent,
      index
    );
    if (amountScaled > discountScaled) {
      _mint(onBehalfOf, (amountScaled - discountScaled).toUint128());
    } else {
      _burn(onBehalfOf, (discountScaled - amountScaled).toUint128());
    }

    _refreshDiscountPercent(
      onBehalfOf,
      super.balanceOf(onBehalfOf).rayMul(index),
      _discountToken.balanceOf(onBehalfOf),
      discountPercent
    );

    uint256 amountToMint = amount + balanceIncrease;
    emit Transfer(address(0), onBehalfOf, amountToMint);
    emit Mint(caller, onBehalfOf, amountToMint, balanceIncrease, index);

    return true;
  }

  
  function _burnScaled(
    address user,
    address target,
    uint256 amount,
    uint256 index
  ) internal override {
    uint256 amountScaled = amount.rayDiv(index);
    require(amountScaled != 0, Errors.INVALID_BURN_AMOUNT);

    uint256 balanceBeforeBurn = balanceOf(user);

    uint256 previousScaledBalance = super.balanceOf(user);
    uint256 discountPercent = _ghoUserState[user].discountPercent;
    (uint256 balanceIncrease, uint256 discountScaled) = _accrueDebtOnAction(
      user,
      previousScaledBalance,
      discountPercent,
      index
    );

    if (amount == balanceBeforeBurn) {
      _burn(user, previousScaledBalance.toUint128());
    } else {
      _burn(user, (amountScaled + discountScaled).toUint128());
    }

    _refreshDiscountPercent(
      user,
      super.balanceOf(user).rayMul(index),
      _discountToken.balanceOf(user),
      discountPercent
    );

    if (balanceIncrease > amount) {
      uint256 amountToMint = balanceIncrease - amount;
      emit Transfer(address(0), user, amountToMint);
      emit Mint(user, user, amountToMint, balanceIncrease, index);
    } else {
      uint256 amountToBurn = amount - balanceIncrease;
      emit Transfer(user, address(0), amountToBurn);
      emit Burn(user, target, amountToBurn, balanceIncrease, index);
    }
  }

  
  function _accrueDebtOnAction(
    address user,
    uint256 previousScaledBalance,
    uint256 discountPercent,
    uint256 index
  ) internal returns (uint256, uint256) {
    uint256 balanceIncrease = previousScaledBalance.rayMul(index) -
      previousScaledBalance.rayMul(_userState[user].additionalData);

    uint256 discountScaled = 0;
    if (balanceIncrease != 0 && discountPercent != 0) {
      uint256 discount = balanceIncrease.percentMul(discountPercent);
      discountScaled = discount.rayDiv(index);
      balanceIncrease = balanceIncrease - discount;
    }

    _userState[user].additionalData = index.toUint128();

    _ghoUserState[user].accumulatedDebtInterest = (balanceIncrease +
      _ghoUserState[user].accumulatedDebtInterest).toUint128();

    return (balanceIncrease, discountScaled);
  }

  
  function _refreshDiscountPercent(
    address user,
    uint256 balance,
    uint256 discountTokenBalance,
    uint256 previousDiscountPercent
  ) internal {
    uint256 newDiscountPercent = _discountRateStrategy.calculateDiscountRate(
      balance,
      discountTokenBalance
    );

    if (previousDiscountPercent != newDiscountPercent) {
      _ghoUserState[user].discountPercent = newDiscountPercent.toUint16();
      emit DiscountPercentUpdated(user, previousDiscountPercent, newDiscountPercent);
    }
  }
}