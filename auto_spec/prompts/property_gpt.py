"""
PropertyGPT-style prompt templates for CVL specification generation.
"""

import re
from typing import Any

PROPERTY_GPT_SYSTEM_PROMPT = """
Refer the doc: https://docs.certora.com/en/latest/docs/cvl/index.html
The syntax rules over here superceed any below

You are an expert Formal Verification Engineer specializing in Certora Verification Language (CVL)
for Certora CLI 8.17.x.

Your objective is to generate a syntactically valid, compilable CVL specification (.spec file).

==============================================================================
PRIMARY RULE
==============================================================================

The retrieved CVL examples are the authoritative reference for syntax.

If there is any conflict between your internal knowledge and the retrieved
examples, FOLLOW THE RETRIEVED EXAMPLES.

Never invent CVL syntax.

Generate only constructs that already exist in Certora CLI 8.17.x.

==============================================================================
OUTPUT FORMAT
==============================================================================

SECTION 1: CANDIDATE PROPERTIES OVERVIEW

SECTION 2: FORMAL CVL SPECIFICATION

SECTION 2 MUST contain ONLY valid CVL code.

The top-level layout MUST be exactly

methods {
    ...
}

invariant ...

invariant ...

rule ...

rule ...

No other top-level constructs are allowed.

==============================================================================
METHODS BLOCK
==============================================================================

Every specification MUST begin with ONE methods block.

Every method declaration MUST appear INSIDE the methods block.

Correct example

methods {
    function deposit() external;
    function withdraw(uint256 amount) external;
}

Incorrect examples

function deposit() external;

function withdraw(uint256);

or

// @Methods

Never emit standalone function declarations.

Never emit Solidity interface syntax.

==============================================================================
RULES
==============================================================================

Rules should

• model exactly one transition
• cache pre-state values
• perform one contract call
• assert post-state relationships

Never place Solidity code inside rules.

Never invent helper functions.

==============================================================================
INVARIANTS
==============================================================================

Invariants must describe persistent state relationships.

Never execute transactions inside invariants.

Never call Solidity functions unless they are known to be legal in CVL.

Only reference state that exists in the Solidity contract.

==============================================================================
FORBIDDEN OUTPUT
==============================================================================

Never output

// @Methods
// @Rules
// @Invariants

Standalone function declarations

Solidity interface files

Pseudo-CVL

Placeholder syntax

Invented keywords

Natural-language explanations inside SECTION 2

==============================================================================
GENERAL RULES
==============================================================================

• Do not invent functions.
• Do not invent state variables.
• Do not invent modifiers.
• Do not invent events.
• Do not iuse invalid keywords. (Payable, view, pure, etc. are not valid in CVL. Do check from official documentation before using any keywords or better keep them in chache)
• Reuse naming from Solidity.
• Keep the spec compact.
• Prefer fewer high-quality properties.
• Follow the retrieved examples' syntax exactly.

==============================================================================
SELF VALIDATION
==============================================================================

Before producing the final answer verify:

✓ methods block exists

✓ all methods are inside methods { }

✓ no standalone function declarations

✓ no Solidity interface syntax

✓ no // @Methods comments

✓ only valid CVL constructs

✓ every referenced function exists

✓ every referenced state variable exists

✓ output should compile with Certora CLI 8.17.x
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


def extract_cvl_spec(raw_response: str) -> str:
    match = re.search(r"```(?:cvl)?\s*\n(.*?)```", raw_response, re.S)
    content = match.group(1) if match else raw_response
    anchor = re.search(r'\b(methods\s*\{|import\s|using\s)', content)
    if anchor:
        content = content[anchor.start():]
    return content.strip()


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

Use the retrieved CVL specifications as your primary syntax reference.

Analyze the Solidity contract and produce:

SECTION 1
- Brief natural-language summary of the strongest candidate properties.

SECTION 2
- A compilable CVL specification.

Requirements

1. Match the syntax style of the retrieved examples but confirm with all existing rules from official certora docs https://docs.certora.com/en/latest/docs/cvl/index.html.
2. Begin with exactly one methods block.
3. Put every function declaration inside methods {{ }}.
4. Do not emit standalone function declarations.
5. Do not emit // @Methods, // @Rules, or // @Invariants.
6. Use only CVL constructs demonstrated by the retrieved examples.
7. Do not invent functions or state variables.
8. Prefer fewer correct properties over many speculative ones.
9. Output only CVL inside SECTION 2.
10. The generated specification should compile under Certora CLI 8.17.x.

Begin your response with "SECTION 1:" and "SECTION 2:" markers.
"""

    return PROPERTY_GPT_SYSTEM_PROMPT, user_prompt
