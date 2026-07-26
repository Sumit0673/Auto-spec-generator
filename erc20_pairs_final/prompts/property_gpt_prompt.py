"""
property_gpt_prompt.py
───────────────────────
PropertyGPT-style prompt templates for drafting CVL invariants and rules.

This module provides version-controlled prompt templates following the
PropertyGPT in-context learning approach for automated smart contract
formal specification generation using Certora Verification Language (CVL).
"""

PROPERTY_GPT_SYSTEM_PROMPT = """You are an expert Formal Verification Engineer specializing in Certora Verification Language (CVL 2.0 / 3.0) and Smart Contract Security.

Your task is to analyze a target Solidity smart contract and draft a complete, syntactically correct, and formal CVL specification (.spec file) containing candidate invariants, rules, and method declarations.

### CVL Specification Architecture & Guidelines

1. **Methods Declaration (`methods` block)**:
   - Identify read-only getters, view/pure functions, and state-modifying functions.
   - Mark view/pure getters as `envfree` where environment parameters (like `msg.sender` or `block.timestamp`) are not needed.
   - Declare wildcard or unresolved external calls appropriately (`function _ ...` or `noprototype`).

2. **Properties to Candidate Drafting (PropertyGPT Approach)**:
   Structure your specification around 4 key property categories:
   - **State Invariants (`invariant`)**: Global state properties that must hold true before and after any transaction (e.g. `totalSupply() == sum of balances`, `balanceOf(user) <= totalSupply()`).
   - **State Transition & Conservational Rules (`rule`)**: Mathematical and functional pre/post-condition rules (e.g. `transfer(to, amount)` decreases sender balance by `amount` and increases recipient balance by `amount`).
   - **Access Control & Authorization Rules (`rule`)**: Restrict state modifications to authorized entities (e.g., non-owners cannot change critical state or pause/mint).
   - **Unitary / Re-entrancy / Solvency Checks (`rule`)**: Ensure zero-address edge cases, overflow safety (using `mathint`), and proper state consistency.

3. **CVL Syntax Standard**:
   - Environment variables must be passed explicitly as `env e` to non-envfree methods.
   - Use `mathint` for math calculations to prevent arithmetic wrapping in specs.
   - Use `ghost` and `hook` if tracking accumulators or global sums across calls.
   - Use `require`, `assert`, and `satisfy` appropriately.

### Output Structure Requirement:
Your output MUST contain two distinct sections:

SECTION 1: CANDIDATE PROPERTIES OVERVIEW
Provide a concise, numbered list of natural language property candidates grouped by category (State Invariants, Transfer & Arithmetic Rules, Access Control Rules).

SECTION 2: FORMAL CVL SPECIFICATION
Provide the raw, production-ready CVL code inside a ```cvl block. Ensure it compiles cleanly with Certora Prover syntax.
"""


def build_in_context_reference_block(retrieved_context: list[dict]) -> str:
    """Formats retrieved vector store chunks into in-context examples for PropertyGPT."""
    if not retrieved_context:
        return "No reference spec chunks retrieved."

    formatted_examples = []
    for idx, chunk in enumerate(retrieved_context, 1):
        contract_name = chunk.get("contract_name", "Unknown Contract")
        spec_filename = chunk.get("spec_filename", "spec.spec")
        similarity_score = chunk.get("score", 0.0)
        document_text = chunk.get("document", "").strip()

        example_str = (
            f"--- Reference Example {idx} | Contract: {contract_name} | Spec File: {spec_filename} (Similarity Score: {similarity_score:.4f}) ---\n"
            f"{document_text}\n"
        )
        formatted_examples.append(example_str)

    return "\n".join(formatted_examples)


def format_property_gpt_prompt(
    contract_code: str,
    retrieved_context: list[dict],
    contract_name: str = "TargetContract"
) -> tuple[str, str]:
    """
    Constructs the (system_prompt, user_prompt) tuple for the PropertyGPT-style CVL generator.

    Args:
        contract_code: Raw Solidity source code of the target contract.
        retrieved_context: List of retrieved CVL spec chunks from vector store.
        contract_name: Optional name identifier for the target contract.

    Returns:
        tuple[str, str]: (system_prompt, user_prompt)
    """
    context_block = build_in_context_reference_block(retrieved_context)

    user_prompt = f"""### RETRIEVED REFERENCE CVL SPECIFICATIONS (IN-CONTEXT EXAMPLES)
Below are high-quality, verified CVL specification excerpts retrieved from similar smart contracts:

{context_block}

================================================================================

### TARGET SOLIDITY CONTRACT: {contract_name}
Below is the Solidity contract source code for which you need to synthesize CVL invariants and rules:

```solidity
{contract_code}
```

================================================================================

### INSTRUCTIONS FOR SPECIFICATION GENERATION
1. Analyze the target Solidity contract's state variables, functions, modifiers, and invariants.
2. Adapt relevant formal specification patterns from the retrieved in-context examples above.
3. First, draft natural language candidate properties (Section 1).
4. Then, write the complete, syntactically correct CVL specification (.spec file) (Section 2).
"""

    return PROPERTY_GPT_SYSTEM_PROMPT, user_prompt
