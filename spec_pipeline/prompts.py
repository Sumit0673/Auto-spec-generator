"""
Stage Prompts - All LLM prompts kept separate for auditability.

Each stage has a distinct prompt with different failure modes:
- Stage 2 (Invariant Mining): Hallucinates importance
- Stage 3 (Rule Writing): Writes N rules when 1 parametric suffices
- Stage 4 (Adversarial Critic): Misses sibling-pattern bugs

Prompt_Builder (Requirement 15)
-------------------------------
The prompt template literals in this module are PROJECT-AGNOSTIC: they contain
no identifier taken from any specific analyzed project. Every example uses
neutral placeholders (``counter`` / ``increment`` / ``<stateVar>`` / ``<setterFn>``).
The concrete struct/enum names, modifier cohorts, and syntax examples that
describe the contract under analysis are assembled at format time by the
``build_*`` helpers below, using ONLY identifiers present in the analyzed source
and the Stage 1 table (R15.1, R15.2, R15.3, R15.7).

``Prompt_Lint`` (spec_pipeline/prompt_lint.py) scans the template literals in
this module for any project-specific identifier and fails the build if one is
found (R15.5, R15.8).

This module is import-safe without slither: it imports nothing heavy.

Brace convention
----------------
System prompt constants (``*_SYSTEM``) are passed to the LLM verbatim and are
NEVER run through ``str.format``; they therefore use plain single braces. The
user-facing ``*_USER_TEMPLATE`` constants ARE run through ``str.format`` and so
escape every literal brace as ``{{``/``}}``. The shared CVL guidance is injected
into the user templates as a ``{cvl_guidance}`` VALUE (never part of the format
template), so its single braces are preserved and its text is character-identical
to the copy embedded in the system prompts (R15.6).
"""

from __future__ import annotations

# ============================================================
# SHARED CVL SYNTAX GUIDANCE (R15.6)
# ============================================================
# Held in ONE definition referenced by the Stage 3 system prompt, the Stage 3
# feedback system prompt, and the Stage 4 repair system prompt. All examples are
# project-neutral (a `counter`/`increment` monotonic example; no project-specific
# identifiers). Uses single braces throughout: it is embedded verbatim in the
# (never-formatted) system prompts and injected as a format VALUE into the user
# templates.

CVL_SYNTAX_GUIDANCE = """CRITICAL CVL 2.0 SYNTAX RULES (from real specs):
- The methods block is DECLARED ONCE at the top of the spec (provided separately above)
- DO NOT redeclare the methods block in your output - JUST WRITE THE RULES (invariants, hooks, rules)
- For parametric rules, use `method f` as parameter (NOT `functionName, args...`):
   rule adminPreservesImmutable(method f) {
       env e;
       require hasRole(DEFAULT_ADMIN_ROLE, e.msg.sender);
       f(e);
       assert DEPLOYER_ROLE == old(DEPLOYER_ROLE);
   }
- For concrete function rules, call the function directly with `env e`:
   rule stateVarMonotonic {
       env e;
       uint256 amount;
       increment(e, amount);
       assert counter() >= old(counter());
   }
- Use `old(var)` to refer to pre-state value in post-condition.
- Ghost variables for monotonic invariants:
   ghost uint256 ghostCounter;
   invariant counterMonotonicGhost() ghostCounter <= counter();
   hook Sstore counter uint256 newVal { ghostCounter = newVal; }  // NOTE: = not :=
- Any struct or enum type declared in the analyzed source is NOT a valid CVL type in the methods block. The struct/enum type names detected in your source are listed in the TYPE GUIDANCE section when present.
- For functions with struct parameters, write separate rules for each field or use primitive field setters
- Hook assignments use `=` NOT `:=` (CVL 2.0 syntax: `ghostVar = newVal;`)
- No tuple comparisons like `(a, b) == (c, d)` - compare each field separately
- Use `havoc` for unbounded variables, `assume` for constraints
- Invariant syntax: `invariant name() condition;` or `invariant name(params) condition;`
- Hook syntax: `hook Sstore varName type newVal { ghostVar = newVal; }`
- Use `=> DISPATCHER(true)` for external calls in methods block, `=> NONDET` for non-deterministic summaries"""


# ============================================================
# STAGE 2: INVARIANT MINING
# ============================================================

STAGE2_SYSTEM = """You are an invariant miner for Solidity smart contracts.
Your ONLY job: classify each state variable into exactly ONE invariant category.

Categories (pick exactly one per variable):
1. immutable-once-set — Set in constructor/initializer, NEVER written after
2. monotonic — Only increases (or only decreases) over time
3. access-gated-write — Only written behind a specific modifier (name it)
4. cross-contract-mirrored — Must stay consistent with another contract's state (name the mirror)
5. free — No invariant (default if unclear)

Output: JSON object mapping contract -> {var_name -> {category, ...}}

Be CONSERVATIVE. If uncertain, use "free". Better to miss an invariant than hallucinate one."""

STAGE2_USER_TEMPLATE = """Analyze these FIRST-PARTY contracts (OZ/libraries already filtered out).

=== STAGE 1 TABLE ===
{stage1_table}

=== RAW SOURCE (for context) ===
```solidity
{source_code}
```

Return JSON with this exact structure:
{{
  "ContractName": {{
    "varName": {{
      "category": "monotonic|access-gated-write|cross-contract-mirrored|immutable-once-set|free",
      "direction": "non-decreasing|non-increasing",  // only for monotonic
      "modifier": "modifierName",                    // only for access-gated-write
      "mirror_contract": "OtherContract",            // only for cross-contract-mirrored
      "mirror_var": "otherVar",                      // only for cross-contract-mirrored
      "rationale": "Why this category",
      "risk_if_violated": "What goes wrong"
    }}
  }}
}}

Only include variables where category != "free" (or include free with empty rationale)."""

# ============================================================
# STAGE 3: PER-CONTRACT RULE WRITING
# ============================================================

STAGE3_SYSTEM = (
    """You are a CVL spec writer for Certora Prover.
Write SPECIFICATIONS, not explanations.

CRITICAL RULES:
1. The invariants below are GIVEN — do NOT rediscover them. Encode them directly.
2. If N functions share the SAME modifier and the SAME invariant pattern, write ONE parametric rule, not N rules.
3. The methods block is PROVIDED SEPARATELY and is ALREADY CORRECT. Do NOT output it again.
4. Output ONLY valid CVL 2.0 syntax in a ```cvl block (rules, invariants, hooks, ghost vars, definitions ONLY).

CVL 2.0 Parametric Rule Syntax - MANDATORY:
rule adminPreservesState(method f) {
    env e;
    require hasRole(DEFAULT_ADMIN_ROLE, e.msg.sender);
    f(e);  // parametric call - calls the method bound to f
    assert DEPLOYER_ROLE == old(DEPLOYER_ROLE);
}

// For invariants with specific function signatures:
rule stateVarMonotonic {
    env e;
    uint256 amount;
    increment(e, amount);
    assert counter() >= old(counter());
}

When N functions sit behind the same access modifier and share one invariant
pattern, do NOT write N separate rules. Write ONE rule parameterized by function
name using `method f`.

"""
    + CVL_SYNTAX_GUIDANCE
)

STAGE3_USER_TEMPLATE = """=== STAGE 1 TABLE (First-Party Only) ===
{stage1_table}

=== STAGE 2 INVARIANTS (GIVEN - DO NOT REDISCOVER) ===
{stage2_invariants}

=== RAW SOURCE ===
```solidity
{source_code}
```

=== METHODS BLOCK (use these exact signatures) ===
{methods_block}
{type_guidance}{cohort_guidance}{syntax_example}
Write CVL spec for each contract. Output ONLY the CVL code in a ```cvl block.

{cvl_guidance}

For each contract:
1. DO NOT include the methods block again (it's global - provided above)
2. Declare ghost variables for invariants (monotonic counters, mirrored state)
3. Write parametric rules for each modifier cohort using `method f`
4. Write concrete rules for specific invariants (like a monotonic counter)
5. Write cross-contract mirroring rules where Stage 2 indicates
6. Use `env e;` in every rule that uses `e.`
7. CVL 2.0 methods block entries MUST end with `;` and CANNOT include `returns` keyword"""

# ============================================================
# STAGE 4: ADVERSARIAL CRITIC
# ============================================================

STAGE4_SYSTEM = """You are an ADVERSARIAL SECURITY CRITIC.
Your job: find bugs the spec writer missed by comparing SIBLING FUNCTIONS.

Look for THESE SPECIFIC PATTERNS:
1. UNCHECKED ZERO-VALUES: Sensitive params (delay, fee, threshold) accepted as 0 without `require(x > 0)`
2. MISSING VALIDITY CHECKS: A check exists in ONE sibling function but NOT in others (e.g. a validity check on one setter but missing on its sibling setters)
3. INCONSISTENT ACCESS CONTROL: A function has different modifier than its neighbors without justification
4. INCONSISTENT VALIDATION PATTERNS: Siblings validate differently (one checks a bound, another doesn't)

Output: JSON array of findings. Each finding:
{{
  "severity": "high|medium|low",
  "type": "zero-value|missing-check|inconsistent-modifier|inconsistent-validation",
  "contract": "ContractName",
  "function": "functionName",
  "param": "paramName",              // for zero-value
  "expected": "expectedCheck(args)", // for missing-check
  "note": "Sibling X does Y; this function does not",
  "sibling": "otherFunctionName"
}}

Be thorough. Compare EVERY external/public function against its siblings."""

STAGE4_USER_TEMPLATE = """=== RAW SOURCE ===
```solidity
{source_code}
```

=== DRAFT SPEC (from Stage 3) ===
```cvl
{draft_spec}
```

=== STAGE 1 TABLE (for sibling comparison) ===
{stage1_table}

Analyze each contract's external/public functions as a COHORT. Compare siblings.
Return ONLY the JSON array of findings."""


# ============================================================
# STAGE 3 FEEDBACK: CertoraRun error-driven re-prompting
# ============================================================

STAGE3_FEEDBACK_SYSTEM = (
    """You are a CVL spec writer for Certora Prover.
You previously wrote a spec that had errors when run through certoraRun.
Your job: FIX the spec based on the error report.

CRITICAL RULES:
1. The invariants below are GIVEN — do NOT rediscover them. Encode them correctly.
2. If N functions share the SAME modifier and the SAME invariant pattern, write ONE parametric rule, not N rules.
3. The methods block is PROVIDED SEPARATELY and is ALREADY CORRECT. Do NOT output it again.
4. Output ONLY valid CVL 2.0 syntax in a ```cvl block (rules, invariants, hooks, ghost vars, definitions ONLY).

COMMON ERROR PATTERNS TO FIX:
- VACUOUS: Precondition never satisfied → weaken require or add ghost setup
- FAILED: Invariant violated → check if rule matches actual contract behavior
- DEAD: Precondition unsatisfiable → fix require logic
- SYNTAX ERROR: Fix CVL 2.0 syntax (method f parameter, old() usage, env e;)

CVL 2.0 Parametric Rule Syntax - MANDATORY:
rule adminPreservesState(method f) {
    env e;
    require hasRole(DEFAULT_ADMIN_ROLE, e.msg.sender);
    f(e);  // parametric call
    assert DEPLOYER_ROLE == old(DEPLOYER_ROLE);
}

For concrete function rules:
rule stateVarMonotonic {
    env e;
    uint256 amount;
    increment(e, amount);
    assert counter() >= old(counter());
}

When N functions sit behind the same access modifier and share one invariant
pattern, do NOT write N separate rules. Write ONE rule parameterized by function
name using `method f`.

"""
    + CVL_SYNTAX_GUIDANCE
)


STAGE3_FEEDBACK_USER_TEMPLATE = """=== STAGE 1 TABLE (First-Party Only) ===
{stage1_table}

=== STAGE 2 INVARIANTS (GIVEN - DO NOT REDISCOVER) ===
{stage2_invariants}

=== RAW SOURCE ===
```solidity
{source_code}
```

=== METHODS BLOCK (use these exact signatures) ===
{methods_block}

=== PREVIOUS SPEC (had errors) ===
```cvl
{previous_spec}
```

=== CERTORA ERROR REPORT ===
{errors_json}
{typecheck_errors_section}
Analyze the errors and write a CORRECTED spec.
Common fixes:
- VACUOUS: Add proper ghost variable setup, or ensure precondition can be satisfied
- FAILED: Verify the invariant actually holds in the contract; adjust if needed
- DEAD: Fix require condition so it can be satisfied
- TIMEOUT: Simplify rule, add --optimistic_loop
- TYPECHECK/SYNTAX: Fix the exact file:line:col the CVL typechecker flagged. For
  "Variable `X` has not been declared. Did you forget to use `sig:` for a method
  selector?", wrap the method selector with `sig:` (e.g. `sig:foo(uint256)`).

Output ONLY the corrected CVL code in a ```cvl block.
"""


# ============================================================
# STAGE 4 → 3 REPAIR: Apply critic findings to spec
# ============================================================

STAGE4_REPAIR_SYSTEM = (
    """You are a CVL spec rewriter.
You receive a draft CVL spec and a list of findings from an adversarial critic.
Your job: APPLY the findings to produce a CORRECTED spec.

Rules:
1. Add missing rules/invariants for each finding
2. Fix inconsistencies flagged by the critic
3. Do NOT remove existing correct rules
4. The methods block is PROVIDED SEPARATELY and is ALREADY CORRECT. Do NOT output it again.
5. Output ONLY valid CVL 2.0 syntax in a ```cvl block (rules, invariants, hooks, ghost vars, definitions ONLY).

"""
    + CVL_SYNTAX_GUIDANCE
)

STAGE4_REPAIR_USER_TEMPLATE = """=== CURRENT SPEC ===
```cvl
{draft_spec}
```

=== CRITIC FINDINGS (apply ALL of these) ===
{findings}

=== RAW SOURCE (for reference) ===
```solidity
{source_code}
```

Output the FULL corrected spec (not just the changes) in a ```cvl block."""


# ============================================================
# Prompt_Builder: per-contract section assembly (R15.1-R15.4, R15.9)
# ============================================================
#
# These helpers build the type-guidance, cohort-guidance, and syntax-example
# sections from the ANALYZED SOURCE and the Stage 1 table, using ONLY identifiers
# present there. They are pure and slither-free (they accept plain data /
# duck-typed objects), so prompts.py stays import-safe.

# Function-gate kinds that carry no ordinary external signature and so cannot
# seed a syntax example.
_SPECIAL_GATE_ATTRS = ("is_constructor", "is_fallback", "is_receive")


def build_type_guidance(struct_enum_types) -> str:
    """Build the type-exclusion guidance section from detected struct/enum names.

    Emits the section ONLY when at least one struct/enum type is detected
    (R15.4). Uses only the provided identifiers (R15.1). Returns an empty string
    when no type is detected, so the surrounding template collapses cleanly.
    """
    names = [str(t) for t in dict.fromkeys(struct_enum_types or []) if str(t).strip()]
    if not names:
        return ""
    joined = ", ".join(names)
    return (
        "\n=== TYPE GUIDANCE (detected in analyzed source) ===\n"
        "The following struct/enum types were detected in the analyzed source and "
        "are NOT valid CVL types in the methods block:\n"
        f"  {joined}\n"
        "For functions using these types, write separate rules per field or use "
        "primitive field setters.\n"
    )


def _gate_is_special(gate) -> bool:
    return any(getattr(gate, attr, False) for attr in _SPECIAL_GATE_ATTRS)


def _gate_modifier(gate) -> str:
    return getattr(gate, "modifier", "none") or "none"


def build_cohort_guidance(contract_entry) -> str:
    """Build cohort guidance naming the modifiers and functions of one contract.

    Uses only the modifier and function identifiers present in the Stage 1 table
    entry for the contract under analysis (R15.2). Groups external/public
    functions by their access modifier so the model can write ONE parametric rule
    per cohort. Returns an empty string when the entry declares no function gates.
    """
    gates = list(getattr(contract_entry, "function_gates", []) or [])
    if not gates:
        return ""

    cohorts: dict[str, list[str]] = {}
    for g in gates:
        if _gate_is_special(g):
            continue
        mod = _gate_modifier(g)
        cohorts.setdefault(mod, [])
        name = getattr(g, "name", "")
        if name and name not in cohorts[mod]:
            cohorts[mod].append(name)

    # Drop empty cohorts (e.g. a contract of only special gates).
    cohorts = {m: fns for m, fns in cohorts.items() if fns}
    if not cohorts:
        return ""

    cname = getattr(contract_entry, "name", "")
    lines = [f"\n=== COHORT GUIDANCE ({cname}) ==="]
    lines.append(
        "Functions grouped by access modifier. When several functions share one "
        "modifier and one invariant pattern, write ONE parametric `method f` rule "
        "for the cohort:"
    )
    for mod in sorted(cohorts):
        fns = ", ".join(cohorts[mod])
        n = len(cohorts[mod])
        lines.append(f"  [{mod}] {n} function(s): {fns}")
    return "\n".join(lines) + "\n"


def _split_top_level(param_body: str) -> list[str]:
    """Split a parameter body on top-level commas (respecting nested brackets)."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in param_body:
        if ch in "([{<":
            depth += 1
            current += ch
        elif ch in ")]}>":
            depth = max(0, depth - 1)
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def _example_from_gate(gate) -> str | None:
    """Render a concrete syntax example call from a function gate signature.

    Uses only the function name and parameter identifiers present in the gate's
    signature (R15.3). Returns None if the gate is special or the signature is
    unusable.
    """
    if _gate_is_special(gate):
        return None
    name = getattr(gate, "name", "")
    signature = getattr(gate, "signature", "") or ""
    if not name:
        return None

    # signature looks like "(type a, type b) -> ret" or "()".
    sig = signature.strip()
    param_body = ""
    if sig.startswith("("):
        close = sig.find(")")
        if close != -1:
            param_body = sig[1:close].strip()

    var_names: list[str] = []
    decl_lines: list[str] = []
    for part in _split_top_level(param_body):
        toks = part.split()
        if len(toks) >= 2:
            var_names.append(toks[-1])
            decl_lines.append(f"    {toks[0]} {toks[-1]};")
        elif toks:
            # Only a type given; synthesize a neutral positional local name.
            local = f"arg{len(var_names)}"
            var_names.append(local)
            decl_lines.append(f"    {toks[0]} {local};")

    call_args = ", ".join(["e"] + var_names) if var_names else "e"
    decls = ("\n" + "\n".join(decl_lines)) if decl_lines else ""

    return (
        "\n=== SYNTAX EXAMPLE (built from an analyzed function) ===\n"
        "```cvl\n"
        f"rule {name}CallExample() {{\n"
        "    env e;"
        f"{decls}\n"
        f"    {name}({call_args});\n"
        "    // assert your invariant here\n"
        "}\n"
        "```\n"
    )


def build_syntax_example(contract_entry) -> str:
    """Build a syntax example from a function signature in the Stage 1 table.

    Selects the first (sorted-by-name) external/public non-special function gate
    and renders a call using only its identifiers (R15.3). Omits the example when
    the contract declares no such function (R15.9).
    """
    gates = list(getattr(contract_entry, "function_gates", []) or [])
    candidates = [
        g
        for g in gates
        if not _gate_is_special(g)
        and getattr(g, "visibility", "external") in ("public", "external")
        and getattr(g, "name", "")
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda g: getattr(g, "name", ""))
    example = _example_from_gate(candidates[0])
    return example or ""


def _per_contract_sections(detected_types, contract_entry) -> tuple[str, str, str]:
    """Assemble the three optional per-contract sections with safe defaults."""
    type_guidance = build_type_guidance(detected_types or [])
    cohort_guidance = ""
    syntax_example = ""
    if contract_entry is not None:
        cohort_guidance = build_cohort_guidance(contract_entry)
        syntax_example = build_syntax_example(contract_entry)
    return type_guidance, cohort_guidance, syntax_example


# ============================================================
# Source packing via the Context_Budgeter (R16.9)
# ============================================================
#
# The former bare `source_code[:50000]` / `draft_spec[:30000]` slices silently
# dropped code on real projects. Source content is now obtained through the
# Context_Budgeter, which applies the configured character budget
# (LLM_MAX_INPUT_CHARS, default 200000), cuts at .sol file boundaries, and
# records any omission instead of truncating (R16.1-R16.6, R16.9).
#
# The budgeter is imported lazily so prompts.py stays import-safe without
# slither (context.py is pure text/data, but the lazy import also keeps the
# module free of any import-time cost).
#
# When callers pass structured inputs (files + analyzed contract + table), full
# priority selection applies. When they pass only raw source (the existing
# callers), the budgeter falls back to a single-file boundary cut and records an
# omission rather than truncating silently. A caller may inspect the omission by
# passing an ``on_omission`` callback that receives the PackResult.


def _load_context_budgeter():
    """Load Context_Budgeter by file path (standalone/slither-absent fallback)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("context.py")
    import sys as _sys

    name = "_spec_pipeline_context"
    if name in _sys.modules:
        return _sys.modules[name].Context_Budgeter
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass field resolution can find the module.
    _sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.Context_Budgeter


def _budget_source(
    source_code: str,
    *,
    files=None,
    analyzed_contract=None,
    table=None,
    table_entry_text: str = "",
    env=None,
    on_omission=None,
) -> str:
    """Return prompt source content packed within the configured budget (R16.9).

    Never applies a bare character slice: the Context_Budgeter records any
    omission. If ``on_omission`` is provided it is invoked with the PackResult
    whenever content was omitted (so the Run_Manifest can record it).
    """
    try:
        from .context import Context_Budgeter
    except ImportError:
        # prompts.py loaded standalone by file path (slither-absent test path):
        # fall back to a direct file-path import so budgeting still works.
        Context_Budgeter = _load_context_budgeter()

    budgeter = Context_Budgeter(env=env)
    result = budgeter.pack_source(
        files=files,
        analyzed_contract=analyzed_contract,
        table=table,
        table_entry_text=table_entry_text,
        raw_source=source_code,
    )
    if on_omission is not None and not result.omission.is_empty:
        on_omission(result)
    return result.text


def _omission_notice(result) -> str:
    """Render the omitted-contract-names notice for inclusion in a prompt (R16.4)."""
    if result is None or result.omission.is_empty:
        return ""
    names = ", ".join(result.omitted_contract_names) or "(unnamed)"
    return (
        "\n=== OMITTED CONTEXT (character budget) ===\n"
        f"The following contracts were omitted to fit the {result.budget}-char "
        f"budget and are NOT shown above: {names}\n"
    )


# ============================================================
# Public format_* helpers (signatures preserved; optional params added)
# ============================================================


def format_stage2_user(
    stage1_table_text: str,
    source_code: str,
    files=None,
    analyzed_contract=None,
    table=None,
) -> str:
    packed = _budget_source(
        source_code,
        files=files,
        analyzed_contract=analyzed_contract,
        table=table,
    )
    return STAGE2_USER_TEMPLATE.format(
        stage1_table=stage1_table_text,
        source_code=packed,
    )


def format_stage3_user(
    stage1_table_text: str,
    stage2_invariants: dict,
    source_code: str,
    methods_block: str,
    detected_types=None,
    contract_entry=None,
    files=None,
    analyzed_contract=None,
    table=None,
) -> str:
    import json

    type_guidance, cohort_guidance, syntax_example = _per_contract_sections(
        detected_types, contract_entry
    )
    packed_result_holder: dict = {}

    def _capture(res):
        packed_result_holder["res"] = res

    packed = _budget_source(
        source_code,
        files=files,
        analyzed_contract=analyzed_contract,
        table=table,
        table_entry_text=stage1_table_text,
        on_omission=_capture,
    )
    notice = _omission_notice(packed_result_holder.get("res"))
    return STAGE3_USER_TEMPLATE.format(
        stage1_table=stage1_table_text,
        stage2_invariants=json.dumps(stage2_invariants, indent=2),
        source_code=packed + notice,
        methods_block=methods_block,
        type_guidance=type_guidance,
        cohort_guidance=cohort_guidance,
        syntax_example=syntax_example,
        cvl_guidance=CVL_SYNTAX_GUIDANCE,
    )


def format_stage4_user(
    source_code: str,
    draft_spec: str,
    stage1_table_text: str,
    files=None,
    analyzed_contract=None,
    table=None,
) -> str:
    packed = _budget_source(
        source_code,
        files=files,
        analyzed_contract=analyzed_contract,
        table=table,
    )
    return STAGE4_USER_TEMPLATE.format(
        source_code=packed,
        draft_spec=draft_spec,
        stage1_table=stage1_table_text,
    )


def format_stage4_repair_user(
    draft_spec: str,
    findings: list,
    source_code: str,
    files=None,
    analyzed_contract=None,
    table=None,
) -> str:
    import json

    packed = _budget_source(
        source_code,
        files=files,
        analyzed_contract=analyzed_contract,
        table=table,
    )
    return STAGE4_REPAIR_USER_TEMPLATE.format(
        draft_spec=draft_spec,
        findings=json.dumps(findings, indent=2, default=str),
        source_code=packed,
    )


def format_stage3_feedback_user(
    stage1_table_text: str,
    stage2_invariants: dict,
    source_code: str,
    methods_block: str,
    previous_spec: str,
    errors: list[dict],
    files=None,
    analyzed_contract=None,
    table=None,
    typecheck_errors: list[dict] | None = None,
) -> str:
    import json

    packed = _budget_source(
        source_code,
        files=files,
        analyzed_contract=analyzed_contract,
        table=table,
        table_entry_text=stage1_table_text,
    )
    return STAGE3_FEEDBACK_USER_TEMPLATE.format(
        stage1_table=stage1_table_text,
        stage2_invariants=json.dumps(stage2_invariants, indent=2),
        source_code=packed,
        methods_block=methods_block,
        previous_spec=previous_spec,
        errors_json=json.dumps(errors, indent=2, default=str),
        typecheck_errors_section=_format_typecheck_errors_section(typecheck_errors),
    )


def _format_typecheck_errors_section(typecheck_errors: list[dict] | None) -> str:
    """Render the concrete CVL typechecker diagnostics for the feedback prompt.

    Each diagnostic is printed as ``file:line:col: message`` so the LLM can fix
    the exact location the keyless local typecheck flagged (e.g. the missing
    ``sig:`` selector). Returns an empty string when there are no typecheck
    errors so the prompt is unchanged for the cloud-verdict feedback path.
    """
    if not typecheck_errors:
        return ""
    lines = ["", "=== CVL TYPECHECKER ERRORS (fix these exactly) ==="]
    for e in typecheck_errors:
        file = e.get("file", "spec")
        line = e.get("line", "?")
        col = e.get("col", "?")
        msg = e.get("message", "")
        lines.append(f"- {file}:{line}:{col}: {msg}")
    lines.append("")
    return "\n".join(lines)
