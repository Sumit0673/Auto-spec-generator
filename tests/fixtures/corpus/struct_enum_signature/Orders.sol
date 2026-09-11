// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Orders {
    enum Status {
        Open,
        Filled,
        Cancelled
    }

    struct Order {
        address maker;
        uint256 amount;
        Status status;
    }

    Order public last;

    function submit(Order calldata order) external {
        last = order;
    }

    function classify(Status status) external pure returns (bool) {
        return status == Status.Open;
    }
}
