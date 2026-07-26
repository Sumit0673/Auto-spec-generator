# 20 real ERC20-focused contract+spec pairs (Certora-verified)

Each numbered folder = one spec + the actual production/harness contract(s) it verifies,
confirmed against the repo's own `.conf` file or `certoraRun` shell script (not guessed —
see "verified via" column).

| # | Folder | Token contract under verification | Spec | Source repo | Verified via |
|---|---|---|---|---|---|
| 01 | AaveTokenV3_erc20 | AaveTokenV3 (AAVE governance token, ERC20) | erc20.spec | bgd-labs/aave-token-v3 | certora/conf/erc20.conf |
| 02 | AaveTokenV3_community | AaveTokenV3 | community.spec | bgd-labs/aave-token-v3 | certora/conf/community.conf |
| 03 | AaveTokenV3_delegate | AaveTokenV3 | delegate.spec | bgd-labs/aave-token-v3 | certora/conf/delegate.conf |
| 04 | AaveTokenV3_general | AaveTokenV3 | general.spec | bgd-labs/aave-token-v3 | certora/conf/general.conf |
| 05 | StakedAaveV3_erc20 | StakedAaveV3 (stkAAVE, ERC20) | token-v3-erc20.spec | bgd-labs/aave-stk-gov-v3 | certora/conf/token-v3-erc20.conf |
| 06 | StakedAaveV3_delegate | StakedAaveV3 | token-v3-delegate.spec | bgd-labs/aave-stk-gov-v3 | certora/conf/token-v3-delegate.conf |
| 07 | StakedAaveV3_community | StakedAaveV3 | token-v3-community.spec | bgd-labs/aave-stk-gov-v3 | certora/conf/token-v3-community.conf |
| 08 | StakedAaveV3_general | StakedAaveV3 | token-v3-general.spec | bgd-labs/aave-stk-gov-v3 | certora/conf/token-v3-general.conf |
| 09 | StakedAaveV3_invariants | StakedAaveV3 | invariants.spec | bgd-labs/aave-stk-gov-v3 | certora/conf/invariants.conf |
| 10 | GhoToken | GhoToken (GHO stablecoin, ERC20) | ghoToken.spec | aave/gho-core | certora/gho/conf/verifyGhoToken.conf |
| 11 | GhoVariableDebtToken | GhoVariableDebtToken (ERC20-based debt token) | ghoVariableDebtToken.spec | aave/gho-core | certora/gho/conf/verifyGhoVariableDebtToken.conf |
| 12 | GhoVariableDebtToken_summarized | GhoVariableDebtToken | ghoVariableDebtToken_summarized.spec | aave/gho-core | certora/gho/conf/verifyGhoVariableDebtToken_summarized.conf |
| 13 | GhoAToken | GhoAToken (interest-bearing ERC20) | ghoAToken.spec | aave/gho-core | certora/gho/conf/verifyGhoAToken.conf |
| 14 | Gho_erc20_helper | GhoTokenHarness (generic ERC20 methods block reused across gho specs) | erc20.spec | aave/gho-core | referenced by multiple gho confs as a helper/import |
| 15 | AStETH_StableDebtToken | StableDebtToken (ERC20-based debt token, Aave V2) | StableDebtToken.spec | MichaelMorami/aave-protocol-v2-AStETH | runStableTokenCLI.sh |
| 16 | AStETH_VariableDebtToken | VariableDebtToken (ERC20-based debt token, Aave V2) | VariableDebtToken.spec | MichaelMorami/aave-protocol-v2-AStETH | runVariableTokenCLI.sh |
| 17 | StakedAaveV1_5_allProps | StakedAaveV3 (earlier stk contract version) | allProps.spec | bgd-labs/aave-stk-v1-5 | certora/scripts/runAllProps.sh |
| 18 | StakedAaveV1_5_invariants | StakedAaveV3 (earlier stk contract version) | invariants.spec | bgd-labs/aave-stk-v1-5 | certora/scripts/runInvariants.sh |
| 19 | Examples_ERC20Full | ERC20.sol (teaching example, not production) | ERC20Full.spec | Certora/Examples | certora/*.conf (verify field) |
| 20 | Examples_ERC4626 | ERC4626.sol (wraps ERC20; solmate) | ERC4626.spec | Certora/Examples | runERC4626Full.conf |

## What "pair" means here
Each folder's `contracts/` holds exactly what the matching `.conf`/script's `files` list
points to — harness + real source where the harness exists, real source alone otherwise.
Nothing in `contracts/` is a guess; every path was cross-checked against the actual repo tree.

## Honest caveats (read before treating this as ground truth)
- **#01–09, #17–18 reuse the same underlying contract across multiple specs.** That's not
  padding — Certora genuinely ran separate spec files against the same harness for different
  rule categories (erc20 correctness vs. delegation vs. invariants). But if you need 20
  *distinct token contracts*, this list only has ~9 distinct contracts, not 20.
- **#14 (Gho_erc20_helper) is a reusable CVL methods-block spec**, not a full top-to-bottom
  verification of one contract — included because you'll see this pattern constantly in
  real Certora repos (shared erc20.spec imported by many `.conf` files) and it's worth
  having as an example, but it's structurally different from the others.
- **#13's harness contract (`certora/gho/munged/...`) doesn't exist in the public repo** —
  the `munged` tree is generated at CI time from a patch, not committed. I substituted the
  real unmunged source (`src/contracts/.../GhoAToken.sol`) instead. Functionally the same
  contract, but not byte-identical to what the prover actually ran against.
- **17/18 (aave-stk-v1-5) and 5-9 (aave-stk-gov-v3) are two different versions of the same
  staking contract** verified at two points in time — real, but overlapping in nature.
- All repos were shallow-cloned at their **current default branch HEAD**, not necessarily the
  exact commit Certora audited (except where I pinned a commit, e.g. static-a-token-v3, which
  I then dropped because that commit had no public specs at all).
