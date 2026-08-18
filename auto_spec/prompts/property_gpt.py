"""
PropertyGPT-style prompt templates for CVL specification generation.
"""

import re
from typing import Any

PROPERTY_GPT_SYSTEM_PROMPT = """
Refer the doc: https://docs.certora.com/en/latest/docs/cvl/index.html
The syntax rules over here superceed any below

CVL SYNTAX RULES:
- Function calls: single argument list, e.g., f(arg1, arg2); never f(x)(y) or f(x)(e, y).
- require statements: write `require condition;` without parentheses around the condition.
- Every rule/invariant that uses `e` must start with `env e;` as first statement.
- Do not add semicolons after invariant or rule blocks.
- Use ghost+hook pattern for aggregates; never sum() on raw mappings.

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

Exception: the retrieved examples are excerpts and may omit the methods block. Methods-block completeness rules below always apply regardless of what retrieved examples show or omit.

==============================================================================
OUTPUT FORMAT
==============================================================================
ONLY and ONLY CVL code should be there in the output.

SECTION 1: FORMAL CVL SPECIFICATION

SECTION 1 MUST contain ONLY valid CVL code.

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
METHODS BLOCK COMPLETENESS (MANDATORY — applies even if retrieved examples omit this)
==============================================================================
Every public state variable and public mapping referenced anywhere in a rule
or invariant MUST have a matching methods block entry for its Solidity-
generated getter. Example:

Solidity:
    uint256 public unlockTime;
    mapping(address => uint256) public deposits;

Required methods block entries:
    function unlockTime() external returns (uint256) envfree;
    function deposits(address) external returns (uint256) envfree;

Never reference unlockTime, deposits, or any other public variable/mapping
as a bare identifier or unregistered function. If it is not declared in
methods{}, it does not exist in CVL, regardless of what the retrieved
examples show.

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
INVARIANTS VS RULE
==============================================================================

Use `invariant` ONLY for properties that hold in every single reachable
state on their own (no before/after comparison).
Use `rule` with explicit pre-state and post-state snapshots for any
property that compares state before and after a function call
(monotonicity, restricted-mutation, conservation, "X only changes via Y").
Never write an invariant body that compares a function's result to a bare
undeclared identifier.

FORBIDDEN inside CVL specs:
- `require` statements WITH string messages: write `require condition;`, NOT `require condition, "msg";` or `require(condition, "msg");`. CVL `require` does NOT accept string error messages.
- `sum()` on non-ghost variables: `sum()` can ONLY be applied to `ghost` variables, never mappings or state variables.
- Bracket indexing on getters: write `deposits(user)`, NOT `deposits[user]`. Public mappings in methods{} are functions.
- `old(...)` or `@old`
- `before` / `after` keywords
- Comparing the same function call to itself (e.g., `f() >= f()`)
- Any pre/post state comparison inside invariant bodies — use a `rule` instead

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

Natural-language explanations mixed into CVL output

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
GHOST + HOOK PATTERN (FIXED EXEMPLAR — use this, not sum() over raw mappings)
==============================================================================

To track aggregates (e.g., sum of all deposits), use a ghost variable with a
hook. NEVER write `sum(deposits)` or similar — that syntax does not exist.

Correct pattern:

    ghost mathint ghostTotalDeposits {
        init_state axiom ghostTotalDeposits == 0;
    }

    hook Sstore deposits[KEY address user] uint256 newVal (uint256 oldVal) {
        ghostTotalDeposits = ghostTotalDeposits + newVal - oldVal;
    }

    invariant totalDepositsTracked()
        to_mathint(totalDeposits()) == ghostTotalDeposits;

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

✓ no `sum()` over raw mappings (use ghost+hook pattern above)

✓ no before/after logic inside invariants (use rules for that)

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
    # Strip reasoning/thinking tags emitted by reasoning models (Nemotron, DeepSeek, etc.)
    cleaned = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.S)
    cleaned = re.sub(r'<reasoning>.*?</reasoning>', '', cleaned, flags=re.S)

    match = re.search(r"```(?:cvl)?\s*\n(.*?)```", cleaned, re.S)
    content = match.group(1) if match else cleaned
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

Analyze the Solidity contract and produce a compilable CVL specification.

Requirements

1. Match the syntax style of the retrieved examples but confirm with all existing rules from official certora docs https://docs.certora.com/en/latest/docs/cvl/index.html.
2. Begin with exactly one methods block.
3. Put every function declaration inside methods {{ }}.
4. Do not emit standalone function declarations.
5. Do not emit // @Methods, // @Rules, or // @Invariants.
6. Use only CVL constructs demonstrated by the retrieved examples.
7. Do not invent functions or state variables.
8. Prefer fewer correct properties over many speculative ones.
9. The generated specification should compile under Certora CLI 8.17.x.

Output ONLY the CVL code inside a ```cvl code block. No natural language, no explanations, no thinking.
"""

    return PROPERTY_GPT_SYSTEM_PROMPT, user_prompt


def format_per_function_prompt(
    contract_code: str,
    function_name: str,
    methods_block: str,
    retrieved_context: list[dict],
    contract_name: str = "TargetContract",
    known_errors: list[str] | None = None,
) -> tuple[str, str]:
    """Prompt for drafting rules for a single function (parallel-safe)."""
    context_block = build_in_context_reference_block(retrieved_context)

    known_errors_block = ""
    if known_errors:
        error_list = "\n".join(f"- {e}" for e in known_errors[:10])
        known_errors_block = (
            "\n### KNOWN-BAD PATTERNS — do not repeat these:\n"
            f"{error_list}\n"
        )

    user_prompt = f"""### METHODS BLOCK (provided — do NOT modify)
```cvl
{methods_block}
```

### REFERENCE EXAMPLES
{context_block}

### TARGET CONTRACT: {contract_name}
```solidity
{contract_code}
```
{known_errors_block}
### TASK
Write CVL rules ONLY for the function `{function_name}`. Do NOT include
a methods block (it is provided above). Do NOT write rules for other
functions. Focus on:
- Pre/post state relationships
- Revert conditions
- Return value correctness

Output ONLY CVL code (rules), no natural language.
"""
    return PROPERTY_GPT_SYSTEM_PROMPT, user_prompt


def format_cross_cutting_prompt(
    contract_code: str,
    function_names: list[str],
    methods_block: str,
    retrieved_context: list[dict],
    contract_name: str = "TargetContract",
    known_errors: list[str] | None = None,
) -> tuple[str, str]:
    """Prompt for cross-cutting invariants spanning the whole contract."""
    context_block = build_in_context_reference_block(retrieved_context)
    fn_list = ", ".join(function_names)

    known_errors_block = ""
    if known_errors:
        error_list = "\n".join(f"- {e}" for e in known_errors[:10])
        known_errors_block = (
            "\n### KNOWN-BAD PATTERNS — do not repeat these:\n"
            f"{error_list}\n"
        )

    user_prompt = f"""### METHODS BLOCK (provided — do NOT modify)
```cvl
{methods_block}
```

### REFERENCE EXAMPLES
{context_block}

### TARGET CONTRACT: {contract_name}
```solidity
{contract_code}
```

Functions: {fn_list}
{known_errors_block}
### TASK
Write CVL invariants and cross-cutting rules that span MULTIPLE functions.
Do NOT include a methods block. Do NOT write per-function rules (those are
handled separately). Focus on:
- State invariants (e.g., totalSupply == sum of balances)
- Conservation laws
- Access-control consistency
- Global solvency / monotonicity properties

Use the ghost+hook pattern for aggregation — never use sum() on raw mappings.

Output ONLY CVL code (invariants and rules), no natural language.
"""
    return PROPERTY_GPT_SYSTEM_PROMPT, user_prompt
