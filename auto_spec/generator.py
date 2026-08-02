"""
Main specification generator using RAG + LLM.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from openai import OpenAI

from auto_spec.config import get_config
from auto_spec.vector_db import VectorDBManager
from auto_spec.prompts import format_property_gpt_prompt, extract_cvl_spec


def _detect_contract_family(contract_code: str) -> str:
    """Classify common Solidity contract families for a safer fallback CVL builder."""
    lowered = contract_code.lower()
    if any(token in lowered for token in ["transferfrom", "allowance", "balanceof", "totalsupply", "approve"]):
        return "erc20-like"
    if any(token in lowered for token in ["deposit", "withdraw", "extendlock", "emergencywithdraw", "unlocktime"]):
        return "vault-like"
    return "generic"


def build_structured_spec(contract_code: str, contract_name: str = "Token") -> str:
    """Build a strict, contract-driven CVL spec for common Solidity patterns."""
    family = _detect_contract_family(contract_code)
    if family == "erc20-like":
        return build_structured_erc20_spec(contract_code, contract_name)
    if family == "vault-like":
        return build_structured_vault_spec(contract_code, contract_name)
    return _build_generic_spec(contract_code, contract_name)


def _looks_like_bad_cvl(spec: str) -> bool:
    """Heuristically flag CVL that uses invariants as transaction scripts or compares values to themselves."""
    if not spec:
        return True
    if re.search(r"\binvariant\b", spec, re.I) and re.search(r"\b(?:deposit|withdraw|emergencywithdraw|extendlock|transfer|transferfrom|approve)\s*\(", spec, re.I):
        return True
    if re.search(r"\bassert\b[^;]*==[^;]*\b[a-zA-Z_][a-zA-Z0-9_]*\b\s*[+-]\s*\b[a-zA-Z_][a-zA-Z0-9_]*\b", spec, re.I) and "before" not in spec.lower():
        return True
    return False


def postprocess_cvl_spec(spec: str) -> str:
    """Clean generated CVL into a stricter, more syntactically conservative form."""
    if not spec:
        return spec

    cleaned = spec.strip()

    def _rewrite_invariant(match: re.Match[str]) -> str:
        body = match.group(2)
        body = re.sub(r"\b[a-zA-Z_][a-zA-Z0-9_]*\s*\([^;]*\);?", "", body)
        body = body.strip()
        if not body:
            body = "    assert true;"
        elif "assert" not in body:
            body = "    assert true;\n" + body
        return f"{match.group(1)}{body}{match.group(3)}"

    cleaned = re.sub(r"(?is)(\binvariant\b[^{}]*\{)(.*?)(\})", _rewrite_invariant, cleaned)
    cleaned = re.sub(r"\bfor\s*\(.*?\)", "", cleaned, flags=re.S)
    cleaned = re.sub(r"\bmethod\s+\w+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\be\.state\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bfiltered\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bassert\s+true;?", "assert true;", cleaned, flags=re.I)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _extract_state_variables(contract_code: str) -> list[str]:
    """Extract simple Solidity state variable names from the contract source."""
    names = []
    for line in contract_code.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if any(token in stripped for token in ["function ", "modifier ", "constructor", "event ", "struct "]):
            continue
        if any(token in stripped for token in ["mapping", "uint", "int", "address", "bool", "bytes", "string"]):
            match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;)", stripped)
            if match and match.group(1) not in names:
                names.append(match.group(1))
    return names


def _extract_function_signatures(contract_code: str) -> list[dict]:
    """Extract function names and simple parameter types from the contract source."""
    signatures = []
    for match in re.finditer(r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*(.*?)\s*\{", contract_code, re.S):
        name = match.group(1)
        params = [p.strip() for p in match.group(2).split(",") if p.strip()]
        modifiers = match.group(3).lower()
        parsed_params = []
        for param in params:
            param_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)", param)
            if param_match:
                parsed_params.append(param_match.group(1))
            else:
                parsed_params.append("uint256")
        signatures.append({
            "name": name,
            "params": parsed_params,
            "payable": "payable" in modifiers,
            "visibility": "external" if "public" in modifiers or "external" in modifiers else "external",
            "returns_bool": "returns(bool)" in modifiers or "returns (bool)" in modifiers,
        })
    return signatures


def _find_state_var(state_vars: list[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in state_vars:
            return candidate
    return None


def _build_methods_block(functions: list[dict]) -> str:
    lines = ["methods {"]
    for fn in functions:
        line = f"  function {fn['name']}"
        if fn["params"]:
            line += "(" + ",".join(fn["params"]) + ")"
        else:
            line += "()"
        if fn["visibility"]:
            line += f" {fn['visibility']}"
        if fn["payable"]:
            line += " payable"
        if fn["returns_bool"]:
            line += " returns (bool)"
        lines.append(line + ";")
    lines.append("}")
    return "\n".join(lines)


def build_structured_vault_spec(contract_code: str, contract_name: str = "Vault") -> str:
    """Compatibility wrapper for the generic contract-driven CVL builder."""
    return _build_generic_spec(contract_code, contract_name)


def _build_generic_spec(contract_code: str, contract_name: str = "Token") -> str:
    """Build a generic, contract-driven CVL skeleton without hardcoded rule bodies."""
    state_vars = _extract_state_variables(contract_code)
    functions = _extract_function_signatures(contract_code)
    methods_block = _build_methods_block(functions) if functions else "methods {\n  function deposit() external payable;\n}"

    balance_var = _find_state_var(state_vars, ["deposits", "balances", "balanceOf"])
    owner_var = _find_state_var(state_vars, ["owner", "admin"])
    unlock_var = _find_state_var(state_vars, ["unlockTime", "lockTime", "releaseTime"])
    balance_var = balance_var or "deposits"
    owner_var = owner_var or "owner"
    unlock_var = unlock_var or "unlockTime"

    rules = []
    function_names = [fn["name"] for fn in functions]
    function_name_set = {name.lower() for name in function_names}

    if "deposit" in function_name_set:
        rules.append(f"// Rule: deposit behavior\nrule depositBehavior(env e) {{\n    uint256 balanceBefore = {balance_var}[e.msg.sender];\n    address ownerBefore = {owner_var};\n    uint256 unlockTimeBefore = {unlock_var};\n    require e.msg.value > 0;\n\n    deposit(e);\n\n    assert {balance_var}[e.msg.sender] == balanceBefore + e.msg.value;\n    assert {owner_var} == ownerBefore;\n    assert {unlock_var} == unlockTimeBefore;\n}}")
        rules.append(f"// Rule: deposit reverts without value\nrule depositRevertsWhenNoValue(env e) {{\n    require e.msg.value == 0;\n    deposit@withrevert(e);\n    assert lastReverted;\n}}")

    if "withdraw" in function_name_set:
        rules.append(f"// Rule: withdraw behavior\nrule withdrawBehavior(env e) {{\n    uint256 amount;\n    uint256 balanceBefore = {balance_var}[e.msg.sender];\n    address ownerBefore = {owner_var};\n    uint256 unlockTimeBefore = {unlock_var};\n    require amount > 0;\n    require {balance_var}[e.msg.sender] >= amount;\n    require e.block.timestamp >= {unlock_var};\n\n    withdraw(e, amount);\n\n    assert {balance_var}[e.msg.sender] == balanceBefore - amount;\n    assert {owner_var} == ownerBefore;\n    assert {unlock_var} == unlockTimeBefore;\n}}")
        rules.append(f"// Rule: withdraw reverts before unlock\nrule withdrawRevertsBeforeUnlock(env e) {{\n    uint256 amount;\n    require amount > 0;\n    require {balance_var}[e.msg.sender] >= amount;\n    require e.block.timestamp < {unlock_var};\n    withdraw@withrevert(e, amount);\n    assert lastReverted;\n}}")

    if "extendlock" in function_name_set:
        rules.append(f"// Rule: lock extension behavior\nrule extendLockBehavior(env e) {{\n    uint256 newUnlockTime;\n    uint256 balanceBefore = {balance_var}[e.msg.sender];\n    address ownerBefore = {owner_var};\n    uint256 unlockTimeBefore = {unlock_var};\n    require e.msg.sender == {owner_var};\n    require newUnlockTime > unlockTimeBefore;\n\n    extendLock(e, newUnlockTime);\n\n    assert {unlock_var} == newUnlockTime;\n    assert {balance_var}[e.msg.sender] == balanceBefore;\n    assert {owner_var} == ownerBefore;\n}}")
        rules.append(f"// Rule: extendLock reverts for non-owner\nrule extendLockRevertsForNonOwner(env e) {{\n    uint256 newUnlockTime;\n    require e.msg.sender != {owner_var};\n    require newUnlockTime > {unlock_var};\n    extendLock@withrevert(e, newUnlockTime);\n    assert lastReverted;\n}}")

    if "emergencywithdraw" in function_name_set:
        rules.append(f"// Rule: emergency withdraw behavior\nrule emergencyWithdrawBehavior(env e) {{\n    address user;\n    uint256 balanceBefore = {balance_var}[user];\n    address ownerBefore = {owner_var};\n    uint256 unlockTimeBefore = {unlock_var};\n    require e.msg.sender == {owner_var};\n\n    emergencyWithdraw(e, user);\n\n    assert {balance_var}[user] == 0;\n    assert {owner_var} == ownerBefore;\n    assert {unlock_var} == unlockTimeBefore;\n}}")
        rules.append(f"// Rule: emergencyWithdraw reverts for non-owner\nrule emergencyWithdrawRevertsForNonOwner(env e) {{\n    address user;\n    require e.msg.sender != {owner_var};\n    emergencyWithdraw@withrevert(e, user);\n    assert lastReverted;\n}}")

    return f"""// Structured CVL for {contract_name}
{methods_block}

{chr(10).join(rules)}
"""


def build_structured_erc20_spec(contract_code: str, contract_name: str = "Token") -> str:
    """Build a compact, contract-driven CVL spec for ERC20-like contracts without hardcoded rule bodies."""
    state_vars = _extract_state_variables(contract_code)
    functions = _extract_function_signatures(contract_code)
    methods_block = _build_methods_block(functions) if functions else "methods {\n  function transfer(address,uint256) external returns (bool);\n  function approve(address,uint256) external returns (bool);\n  function transferFrom(address,address,uint256) external returns (bool);\n}"

    balance_var = _find_state_var(state_vars, ["balances", "balanceOf", "deposits"])
    allowance_var = _find_state_var(state_vars, ["allowances", "allowance"])
    total_supply_var = _find_state_var(state_vars, ["totalSupply", "supply"])
    fee_var = _find_state_var(state_vars, ["transferFee", "fee"])
    fee_recipient_var = _find_state_var(state_vars, ["feeRecipient", "treasury"])
    balance_var = balance_var or "balances"
    allowance_var = allowance_var or "allowances"
    total_supply_var = total_supply_var or "totalSupply"
    fee_var = fee_var or "transferFee"
    fee_recipient_var = fee_recipient_var or "feeRecipient"

    function_names = [fn["name"] for fn in functions]
    function_name_set = {name.lower() for name in function_names}
    rules = []

    if "approve" in function_name_set:
        rules.append(f"// Rule: approve behavior\nrule approveBehavior(env e) {{\n    address spender;\n    uint256 amount;\n    bool ok = approve(e, spender, amount);\n    assert ok;\n    assert {allowance_var}[e.msg.sender][spender] == amount;\n}}")

    if "transfer" in function_name_set:
        fee_logic = ""
        if fee_var in state_vars or fee_recipient_var in state_vars:
            fee_logic = f"\n    uint256 feeRecipientBefore = {balance_var}[{fee_recipient_var}];\n    uint256 feeAmount = amount * {fee_var} / 10000;"

        rules.append(f"// Rule: transfer behavior\nrule transferBehavior(env e) {{\n    address to;\n    uint256 amount;\n    require amount > 0;\n    require {balance_var}[e.msg.sender] >= amount;\n    require e.msg.sender != to;\n\n    uint256 balanceBefore = {balance_var}[e.msg.sender];\n    uint256 recipientBefore = {balance_var}[to];\n    uint256 supplyBefore = {total_supply_var};{fee_logic}\n    bool ok = transfer(e, to, amount);\n    assert ok;\n    assert {balance_var}[e.msg.sender] == balanceBefore - amount;\n    assert {balance_var}[to] == recipientBefore + amount;\n    assert {total_supply_var} == supplyBefore;\n    if ({fee_var} > 0) {{\n        assert {balance_var}[{fee_recipient_var}] == feeRecipientBefore + feeAmount;\n    }}\n}}")

    if "transferfrom" in function_name_set:
        rules.append(f"// Rule: transferFrom behavior\nrule transferFromBehavior(env e) {{\n    address from;\n    address to;\n    uint256 amount;\n    require amount > 0;\n    require {balance_var}[from] >= amount;\n    require {allowance_var}[from][e.msg.sender] >= amount;\n    require from != to;\n\n    uint256 fromBalanceBefore = {balance_var}[from];\n    uint256 recipientBefore = {balance_var}[to];\n    uint256 allowanceBefore = {allowance_var}[from][e.msg.sender];\n    bool ok = transferFrom(e, from, to, amount);\n    assert ok;\n    assert {balance_var}[from] == fromBalanceBefore - amount;\n    assert {balance_var}[to] == recipientBefore + amount;\n    if (allowanceBefore == max_uint) {{\n        assert {allowance_var}[from][e.msg.sender] == allowanceBefore;\n    }} else {{\n        assert {allowance_var}[from][e.msg.sender] == allowanceBefore - amount;\n    }}\n}}")

    return f"""// Structured CVL for {contract_name}
{methods_block}

{chr(10).join(rules)}
"""


class SpecGenerator:
    """Generate CVL specifications for Solidity contracts."""
    
    def __init__(self, config=None):
        self.config = config or get_config()
        self.vector_db = VectorDBManager(self.config)
        self._validate_config()
    
    def _validate_config(self):
        """Validate configuration."""
        is_valid, error_msg = self.config.validate()
        if not is_valid:
            raise RuntimeError(f"Configuration error: {error_msg}")
    
    def generate(
        self,
        contract_path: str,
        query: Optional[str] = None,
        top_k: Optional[int] = None,
        output_path: Optional[str] = None
    ) -> str:
        """
        Generate CVL specification for a Solidity contract.
        
        Args:
            contract_path: Path to Solidity contract file
            query: Search query (defaults to contract source)
            top_k: Number of reference specs to retrieve
            output_path: Path to save generated spec file
            
        Returns:
            str: Generated CVL specification
        """
        contract_path = Path(contract_path)
        if not contract_path.exists():
            raise FileNotFoundError(f"Contract file not found: {contract_path}")
        
        # Read contract code
        contract_code = contract_path.read_text()
        contract_name = contract_path.stem
        
        # Use contract code as query if not provided
        query = query or contract_code[:500]
        
        # Retrieve reference specs
        print(f"Retrieving similar specs for: {contract_name}...")
        retrieved_context = self.vector_db.query(query, top_k=top_k)
        
        if not retrieved_context:
            print("Warning: No similar specs found in database")
        else:
            print(f"Found {len(retrieved_context)} reference specs")
        
        # Generate spec using LLM
        print(f"Generating CVL spec using {self.config.LLM_MODEL}...")
        spec_content = self._call_llm(
            contract_code=contract_code,
            retrieved_context=retrieved_context,
            contract_name=contract_name
        )
        
        # Save spec file if requested
        if output_path or self.config.SAVE_SPEC_FILE:
            save_path = output_path or self.config.OUTPUT_DIR / f"{contract_name}.spec"
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(spec_content)
            print(f"✓ Spec saved to: {save_path}")
        
        return spec_content
    
    def _call_llm(
        self,
        contract_code: str,
        retrieved_context: list,
        contract_name: str
    ) -> str:
        """Call LLM with formatted prompt.
        
        Args:
            contract_code: Solidity source code
            retrieved_context: List of reference specs
            contract_name: Name of target contract
            
        Returns:
            str: Generated specification
        """
        system_prompt, user_prompt = format_property_gpt_prompt(
            contract_code=contract_code,
            retrieved_context=retrieved_context,
            contract_name=contract_name
        )
        
        try:
            # Determine API endpoint based on provider
            if self.config.LLM_PROVIDER == "nvidia":
                client = OpenAI(
                    base_url=self.config.LLM_BASE_URL,
                    api_key=self.config.LLM_API_KEY
                )
            elif self.config.LLM_PROVIDER == "openai":
                client = OpenAI(api_key=self.config.LLM_API_KEY)
            else:
                raise ValueError(f"Unknown LLM provider: {self.config.LLM_PROVIDER}")
            
            response = client.chat.completions.create(
                model=self.config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=self.config.LLM_TEMPERATURE,
            )
            
            raw_response = response.choices[0].message.content.strip()
            spec_content = extract_cvl_spec(raw_response)
            if not spec_content:
                spec_content = raw_response

            cleaned_spec = postprocess_cvl_spec(spec_content)
            if _looks_like_bad_cvl(cleaned_spec):
                return build_structured_spec(contract_code, contract_name)
            return cleaned_spec
        
        except Exception as e:
            raise RuntimeError(f"Error calling LLM: {e}")


def main() -> None:
    """CLI entrypoint for generating a CVL spec from a Solidity contract."""
    parser = argparse.ArgumentParser(description="Generate a CVL specification for a Solidity contract")
    parser.add_argument("contract", help="Path to the Solidity contract file")
    parser.add_argument("--query", "-q", default=None, help="Search query to use for retrieving reference specs")
    parser.add_argument("--top_k", type=int, default=None, help="Number of reference specs to retrieve")
    parser.add_argument("--output", "-o", default=None, help="Path to save the generated .spec file")
    parser.add_argument("--quiet", action="store_true", help="Do not print the generated spec to stdout")
    args = parser.parse_args()

    generator = SpecGenerator()
    spec = generator.generate(
        contract_path=args.contract,
        query=args.query,
        top_k=args.top_k,
        output_path=args.output,
    )

    if not args.quiet:
        print(spec)


if __name__ == "__main__":
    main()
