pragma solidity ^0.8.10;

import {IERC20} from '@aave/core-v3/contracts/dependencies/openzeppelin/contracts/IERC20.sol';
import {GPv2SafeERC20} from '@aave/core-v3/contracts/dependencies/gnosis/contracts/GPv2SafeERC20.sol';
import {VersionedInitializable} from '@aave/core-v3/contracts/protocol/libraries/aave-upgradeability/VersionedInitializable.sol';
import {Errors} from '@aave/core-v3/contracts/protocol/libraries/helpers/Errors.sol';
import {WadRayMath} from '@aave/core-v3/contracts/protocol/libraries/math/WadRayMath.sol';
import {IPool} from '@aave/core-v3/contracts/interfaces/IPool.sol';
import {IAToken} from '@aave/core-v3/contracts/interfaces/IAToken.sol';
import {IAaveIncentivesController} from '@aave/core-v3/contracts/interfaces/IAaveIncentivesController.sol';
import {IInitializableAToken} from '@aave/core-v3/contracts/interfaces/IInitializableAToken.sol';
import {ScaledBalanceTokenBase} from '@aave/core-v3/contracts/protocol/tokenization/base/ScaledBalanceTokenBase.sol';
import {IncentivizedERC20} from '@aave/core-v3/contracts/protocol/tokenization/base/IncentivizedERC20.sol';
import {EIP712Base} from '@aave/core-v3/contracts/protocol/tokenization/base/EIP712Base.sol';
import {IGhoToken} from '../../../gho/interfaces/IGhoToken.sol';
import {IGhoFacilitator} from '../../../gho/interfaces/IGhoFacilitator.sol';
import {IGhoAToken} from './interfaces/IGhoAToken.sol';
import {GhoVariableDebtToken} from './GhoVariableDebtToken.sol';

contract GhoAToken is VersionedInitializable, ScaledBalanceTokenBase, EIP712Base, IGhoAToken {
  using WadRayMath for uint256;
  using GPv2SafeERC20 for IERC20;

  bytes32 public constant PERMIT_TYPEHASH =
    keccak256('Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)');

  uint256 public constant ATOKEN_REVISION = 0x1;

  address internal _treasury;
  address internal _underlyingAsset;
  GhoVariableDebtToken internal _ghoVariableDebtToken;
  address internal _ghoTreasury;
  function getRevision() internal pure virtual override returns (uint256) {
    return ATOKEN_REVISION;
  }

  
  constructor(
    IPool pool
  ) ScaledBalanceTokenBase(pool, 'GHO_ATOKEN_IMPL', 'GHO_ATOKEN_IMPL', 0) EIP712Base() {
  }
  function initialize(
    IPool initializingPool,
    address treasury,
    address underlyingAsset,
    IAaveIncentivesController incentivesController,
    uint8 aTokenDecimals,
    string calldata aTokenName,
    string calldata aTokenSymbol,
    bytes calldata params
  ) external override initializer {
    require(initializingPool == POOL, Errors.POOL_ADDRESSES_DO_NOT_MATCH);
    _setName(aTokenName);
    _setSymbol(aTokenSymbol);
    _setDecimals(aTokenDecimals);

    _treasury = treasury;
    _underlyingAsset = underlyingAsset;
    _incentivesController = incentivesController;

    _domainSeparator = _calculateDomainSeparator();

    emit Initialized(
      underlyingAsset,
      address(POOL),
      treasury,
      address(incentivesController),
      aTokenDecimals,
      aTokenName,
      aTokenSymbol,
      params
    );
  }
  function mint(
    address caller,
    address onBehalfOf,
    uint256 amount,
    uint256 index
  ) external virtual override onlyPool returns (bool) {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }
  function burn(
    address from,
    address receiverOfUnderlying,
    uint256 amount,
    uint256 index
  ) external virtual override onlyPool {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }
  function mintToTreasury(uint256 amount, uint256 index) external virtual override onlyPool {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }
  function transferOnLiquidation(
    address from,
    address to,
    uint256 value
  ) external virtual override onlyPool {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }
  function balanceOf(
    address user
  ) public view virtual override(IncentivizedERC20, IERC20) returns (uint256) {
    return 0;
  }
  function totalSupply() public view virtual override(IncentivizedERC20, IERC20) returns (uint256) {
    return 0;
  }
  function RESERVE_TREASURY_ADDRESS() external view override returns (address) {
    return _treasury;
  }
  function UNDERLYING_ASSET_ADDRESS() external view override returns (address) {
    return _underlyingAsset;
  }

  
  function transferUnderlyingTo(address target, uint256 amount) external virtual override onlyPool {
    IGhoToken(_underlyingAsset).mint(target, amount);
  }
  function handleRepayment(
    address user,
    address onBehalfOf,
    uint256 amount
  ) external virtual override onlyPool {
    uint256 balanceFromInterest = _ghoVariableDebtToken.getBalanceFromInterest(onBehalfOf);
    if (amount <= balanceFromInterest) {
      _ghoVariableDebtToken.decreaseBalanceFromInterest(onBehalfOf, amount);
    } else {
      _ghoVariableDebtToken.decreaseBalanceFromInterest(onBehalfOf, balanceFromInterest);
      IGhoToken(_underlyingAsset).burn(amount - balanceFromInterest);
    }
  }
  function distributeFeesToTreasury() external virtual override {
    uint256 balance = IERC20(_underlyingAsset).balanceOf(address(this));
    IERC20(_underlyingAsset).transfer(_ghoTreasury, balance);
    emit FeesDistributedToTreasury(_ghoTreasury, _underlyingAsset, balance);
  }
  function permit(
    address owner,
    address spender,
    uint256 value,
    uint256 deadline,
    uint8 v,
    bytes32 r,
    bytes32 s
  ) external override {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }

  
  function _transfer(address from, address to, uint128 amount) internal override {
    revert(Errors.OPERATION_NOT_SUPPORTED);
  }

  
  function DOMAIN_SEPARATOR() public view override(IAToken, EIP712Base) returns (bytes32) {
    return super.DOMAIN_SEPARATOR();
  }

  
  function nonces(address owner) public view override(IAToken, EIP712Base) returns (uint256) {
    return super.nonces(owner);
  }
  function _EIP712BaseId() internal view override returns (string memory) {
    return name();
  }
  function rescueTokens(address token, address to, uint256 amount) external override onlyPoolAdmin {
    require(token != _underlyingAsset, Errors.UNDERLYING_CANNOT_BE_RESCUED);
    IERC20(token).safeTransfer(to, amount);
  }
  function setVariableDebtToken(address ghoVariableDebtToken) external override onlyPoolAdmin {
    require(address(_ghoVariableDebtToken) == address(0), 'VARIABLE_DEBT_TOKEN_ALREADY_SET');
    require(ghoVariableDebtToken != address(0), 'ZERO_ADDRESS_NOT_VALID');
    _ghoVariableDebtToken = GhoVariableDebtToken(ghoVariableDebtToken);
    emit VariableDebtTokenSet(ghoVariableDebtToken);
  }
  function getVariableDebtToken() external view override returns (address) {
    return address(_ghoVariableDebtToken);
  }
  function updateGhoTreasury(address newGhoTreasury) external override onlyPoolAdmin {
    require(newGhoTreasury != address(0), 'ZERO_ADDRESS_NOT_VALID');
    address oldGhoTreasury = _ghoTreasury;
    _ghoTreasury = newGhoTreasury;
    emit GhoTreasuryUpdated(oldGhoTreasury, newGhoTreasury);
  }
  function getGhoTreasury() external view override returns (address) {
    return _ghoTreasury;
  }
}