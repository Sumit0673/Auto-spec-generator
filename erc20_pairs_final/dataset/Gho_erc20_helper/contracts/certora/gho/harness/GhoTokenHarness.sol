pragma solidity ^0.8.0;

import {IGhoToken} from '../munged/contracts/gho/interfaces/IGhoToken.sol';
import '@openzeppelin/contracts/utils/structs/EnumerableSet.sol';
import {GhoToken} from '../munged/contracts/gho/GhoToken.sol';

contract GhoTokenHarness is GhoToken {
  using EnumerableSet for EnumerableSet.AddressSet;

  constructor() GhoToken(msg.sender) {}

  
  function getFacilitatorBucketCapacity(address facilitator) public view returns (uint256) {
    (uint256 bucketCapacity, ) = getFacilitatorBucket(facilitator);
    return bucketCapacity;
  }

  
  function getFacilitatorBucketLevel(address facilitator) public view returns (uint256) {
    (, uint256 bucketLevel) = getFacilitatorBucket(facilitator);
    return bucketLevel;
  }

  
  function getFacilitatorsListLen() external view returns (uint256) {
    address[] memory flist = getFacilitatorsList();
    return flist.length;
  }

  
  function is_in_facilitator_mapping(address addr) external view returns (bool) {
    Facilitator memory facilitator = _facilitators[addr];
    return facilitator.isLabelNonempty;
  }

  
  function is_in_facilitator_set_map(address addr) external view returns (bool) {
    return _facilitatorsList.contains(addr);
  }

  
  function is_in_facilitator_set_array(address addr) external view returns (bool) {
    address[] memory flist = getFacilitatorsList();
    for (uint256 i = 0; i < flist.length; ++i) {
      if (address(flist[i]) == addr) {
        return true;
      }
    }
    return false;
  }

  
  function to_bytes32(address value) external pure returns (bytes32 b) {
    b = bytes32(uint256(uint160(value)));
  }
}