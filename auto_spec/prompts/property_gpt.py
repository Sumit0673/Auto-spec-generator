"""
PropertyGPT-style prompt templates for CVL specification generation.
"""

import re
from typing import Any

PROPERTY_GPT_SYSTEM_PROMPT = """You are an expert Formal Verification Engineer specializing in Certora Verification Language (CVL 2.x/3.x) and smart contract security.

Your task is to analyze a Solidity contract and draft a high-quality, syntactically plausible CVL specification (.spec file) with meaningful invariants, rules, and method declarations.

### CVL Writing Guidelines
1. Prefer a small set of high-signal properties over speculative or redundant ones.
2. Use invariants for state properties that should hold across all transitions.
3. Use rules for authorization, conservation, and transition behavior.
4. Invariants must describe state relationships; they must not call contract functions or execute transactions.
5. Rules should model one transition at a time and should cache pre-state values before asserting post-state changes.
6. Use the methods block to document the contract interface and to mark view/pure getters as envfree when appropriate.
7. Use mathint for arithmetic that can exceed uint256 bounds or for conservation checks.
8. Do not invent functions, state variables, modifiers, or events that are absent from the Solidity source.
9. Be conservative with external calls and unresolved behavior; prefer precise summaries and avoid over-claiming.
10. Favor explicit, readable CVL over overly complex hooks unless the contract clearly requires them.
11. Generate strict CVL syntax only: use valid Certora constructs, correct rule and invariant syntax, and proper method declarations.
12. Do not output pseudo-CVL, shorthand, or informal placeholders; every rule, invariant, and method must be syntactically valid and compilable.
13. Keep the output minimal, deterministic, and focused on concrete properties that directly match the Solidity source.

### Expected Output Shape
- SECTION 1: CANDIDATE PROPERTIES OVERVIEW
- SECTION 2: FORMAL CVL SPECIFICATION

The CVL code should be minimal, compilable, and focused on the most informative properties for the target contract. Return only strict CVL syntax that is valid for Certora and avoids pseudo-code or informal placeholders.
"""


def build_in_context_reference_block(retrieved_context: list[dict]) -> str:
    """Format retrieved vector store chunks into in-context examples."""
    if not retrieved_context:
        return "No reference spec chunks retrieved."

    formatted_examples = []
    for idx, chunk in enumerate(retrieved_context, 1):
        contract_name = chunk.get("contract_name", "Unknown Contract")
        spec_filename = chunk.get("spec_filename", "spec.spec")
        similarity_score = chunk.get("score", 0.0)
        document_text = chunk.get("document", "").strip()

        example_str = (
            f"--- Reference Example {idx} | Contract: {contract_name} | "
            f"Spec File: {spec_filename} (Similarity: {similarity_score:.4f}) ---\n"
            f"{document_text}\n"
        )
        formatted_examples.append(example_str)

    return "\n".join(formatted_examples)


def analyze_solidity_contract(contract_code: str, contract_name: str = "TargetContract") -> dict[str, Any]:
    """Extract a lightweight contract fingerprint to guide the CVL prompt."""
    lowered = contract_code.lower()
    function_names = re.findall(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", contract_code)
    modifier_names = re.findall(r"\bmodifier\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", contract_code)
    state_var_names = []

    for line in contract_code.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if any(token in stripped for token in ["function ", "modifier ", "constructor", "event ", "struct "]):
            continue
        if any(token in stripped for token in ["uint", "int", "address", "bool", "bytes", "string", "mapping"]):
            match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;)", stripped)
            if match:
                state_var_names.append(match.group(1))

    state_variable_summary = {name: True for name in state_var_names}
    risk_signals: list[str] = []

    if any(name in lowered for name in ["transferfrom", "allowance", "balanceof", "totalsupply"]):
        risk_signals.append("balance_tracking")
    if any(name in lowered for name in ["onlyowner", "onlyrole", "owner", "admin"]):
        risk_signals.append("access_control")
    if any(name in lowered for name in ["mint", "burn", "supply"]):
        risk_signals.append("supply_management")
    if "call(" in lowered or "delegatecall" in lowered:
        risk_signals.append("external_calls")

    contract_family = "generic"
    if any(name in lowered for name in ["transfer", "transferfrom", "allowance", "balanceof", "totalsupply"]):
        contract_family = "erc20-like"
    elif any(name in lowered for name in ["owner", "onlyowner", "admin", "role"]):
        contract_family = "access-control"

    property_hints = []
    if contract_family == "erc20-like":
        property_hints.extend([
            "Track conservation of balances and total supply",
            "Ensure approval and transferFrom flows do not bypass intended authorization",
            "Check that unauthorized accounts cannot change balances or allowances",
        ])
    elif contract_family == "access-control":
        property_hints.extend([
            "Protect privileged state changes behind authorized roles",
            "Ensure ownership changes do not create inconsistent access paths",
        ])
    else:
        property_hints.append("Focus on state invariants and transition safety for the core state variables")

    return {
        "contract_name": contract_name,
        "contract_family": contract_family,
        "state_variables": state_variable_summary,
        "function_names": function_names,
        "modifier_names": modifier_names,
        "risk_signals": risk_signals,
        "property_hints": property_hints,
    }


def extract_cvl_spec(response: str) -> str:
    """Extract the CVL code block from an LLM response and normalize it for downstream use."""
    if not response:
        return ""

    section_match = re.search(r"SECTION\s+2:\s*(.*)", response, re.S | re.I)
    if section_match:
        response = section_match.group(1)

    fence_match = re.search(r"```(?:cvl|spec)?\s*(.*?)```", response, re.S | re.I)
    if fence_match:
        return fence_match.group(1).strip()

    if "methods" in response or "invariant" in response or "rule" in response:
        return response.strip()

    return ""


def format_property_gpt_prompt(
    contract_code: str,
    retrieved_context: list[dict],
    contract_name: str = "TargetContract"
) -> tuple[str, str]:
    """
    Construct the (system_prompt, user_prompt) tuple for PropertyGPT-style CVL generation.

    Args:
        contract_code: Raw Solidity source code of the target contract.
        retrieved_context: List of retrieved CVL spec chunks from vector store.
        contract_name: Optional name identifier for the target contract.

    Returns:
        tuple[str, str]: (system_prompt, user_prompt)
    """
    context_block = build_in_context_reference_block(retrieved_context)
    analysis = analyze_solidity_contract(contract_code, contract_name)

    analysis_block = []
    analysis_block.append(f"- Contract family: {analysis['contract_family']}")
    analysis_block.append(f"- State variables detected: {', '.join(analysis['state_variables'].keys()) or 'none'}")
    analysis_block.append(f"- Relevant functions: {', '.join(analysis['function_names'][:10]) or 'none'}")
    analysis_block.append(f"- Risk signals: {', '.join(analysis['risk_signals']) or 'none'}")
    if analysis['property_hints']:
        analysis_block.append("- Suggested property emphasis:")
        analysis_block.extend([f"  - {hint}" for hint in analysis['property_hints']])

    user_prompt = f"""### RETRIEVED REFERENCE CVL SPECIFICATIONS (IN-CONTEXT EXAMPLES)
Below are high-quality, verified CVL specification excerpts retrieved from similar smart contracts:

{context_block}

================================================================================

### CONTRACT ANALYSIS
Use the following contract fingerprint to guide the spec:

{chr(10).join(analysis_block)}

================================================================================

### TARGET SOLIDITY CONTRACT: {contract_name}
Below is the Solidity contract source code for which you need to synthesize CVL invariants and rules:

```solidity
{contract_code}
```

================================================================================

### TASK
1. Analyze the Solidity code and the retrieved CVL references.
2. Produce SECTION 1 with concise natural-language property candidates grouped by category.
3. Produce SECTION 2 with a compact, compilable CVL specification using strict Certora CVL syntax.
4. Keep the spec grounded in the contract source; do not invent methods, state variables, or access patterns.
5. Model invariants as state relationships only; do not call Solidity functions inside invariants.
6. For transition rules, cache pre-state values before making assertions about post-state values.
7. Do not output pseudo-CVL, shorthand, or informal placeholders; ensure every rule, invariant, and method declaration is syntactically valid.

Begin your response with "SECTION 1:" and "SECTION 2:" markers.
"""

    return PROPERTY_GPT_SYSTEM_PROMPT, user_prompt
