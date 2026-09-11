// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IOracle {
    function latestPrice() external view returns (uint256);

    function decimals() external view returns (uint8);
}

interface IFeed {
    function update(uint256 price) external;
}
