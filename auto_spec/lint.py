from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LintError:
    category: str
    message: str
    line: int | None = None


def lint_spec(spec_content: str, contract_code: str) -> list[LintError]:
    """Run all lint checks. Returns empty list if clean."""
    errors: list[LintError] = []
    errors.extend(_check_keyword_leak(spec_content))
    errors.extend(_check_getter_completeness(spec_content, contract_code))
    errors.extend(_check_forbidden_constructs(spec_content))
    errors.extend(_check_invariant_shape(spec_content))
    errors.extend(_check_undeclared_env(spec_content))
    errors.extend(_check_mapping_indexing_syntax(spec_content, contract_code))
    errors.extend(_check_uncalled_getters(spec_content, contract_code))
    errors.extend(_check_invalid_require(spec_content))
    errors.extend(_check_invalid_require_invariant(spec_content))
    errors.extend(_check_bare_solidity_env(spec_content))
    errors.extend(_check_invariant_brace_body(spec_content))
    errors.extend(_check_env_in_invariant(spec_content))
    errors.extend(_check_undeclared_ghost(spec_content))
    errors.extend(_check_sinvoke(spec_content))
    errors.extend(_check_envfree_with_env(spec_content))
    errors.extend(_check_require_parens(spec_content))
    errors.extend(_check_missing_semicolon(spec_content))
    return errors


def _extract_methods_block(spec: str) -> str | None:
    """Return the content inside methods { ... }, or None if missing."""
    match = re.search(r'methods\s*\{', spec)
    if not match:
        return None
    depth, start = 1, match.end()
    for i in range(start, len(spec)):
        if spec[i] == '{':
            depth += 1
        elif spec[i] == '}':
            depth -= 1
            if depth == 0:
                return spec[start:i]
    return None


def _check_keyword_leak(spec: str) -> list[LintError]:
    """Check 1: Solidity mutability keywords inside methods block."""
    methods = _extract_methods_block(spec)
    if methods is None:
        return [LintError("missing_methods", "No methods block found in spec")]
    errors = []
    for i, line in enumerate(methods.splitlines(), 1):
        for kw in ("view", "pure", "payable", "nonpayable"):
            if re.search(rf'\b{kw}\b', line):
                errors.append(LintError(
                    "keyword_leak",
                    f"Solidity keyword `{kw}` in methods block: {line.strip()}",
                    line=i,
                ))
    return errors


def _mapping_var_names(contract_code: str) -> list[str]:
    """Names of public mappings, robust to nested and array-valued mappings.

    Scans each `mapping(` for its matching closing paren (any nesting depth),
    then expects `public <name>`. Non-public mappings produce no getter, so they
    are intentionally skipped.
    """
    names: list[str] = []
    for m in re.finditer(r"\bmapping\s*\(", contract_code):
        depth, i = 1, m.end()
        while i < len(contract_code) and depth:
            ch = contract_code[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        rest = contract_code[i:]
        mm = re.match(r"\s+public\s+(?:constant\s+)?(\w+)", rest)
        if mm:
            names.append(mm.group(1))
    return names


def _parse_public_declarations(contract_code: str) -> list[tuple[str, str]]:
    """Parse public state vars and mappings from Solidity source.

    Returns list of (name, kind) where kind is 'variable' or 'mapping'.
    Handles builtin AND custom types (structs, enums, contract types, arrays),
    so contracts like `IERC20 public token` or `Foo[] public list` are not
    silently invisible to the getter-completeness checks.
    """
    decls: list[tuple[str, str]] = []
    for name in _mapping_var_names(contract_code):
        decls.append((name, "mapping"))
    seen = {name for name, _ in decls}
    # builtin scalar types (exact match)
    for m in re.finditer(
        r"\b(?:(uint\d*|int\d*|address|bool|bytes\d*|string))\s+public\s+(?:constant\s+)?(\w+)",
        contract_code,
    ):
        name = m.group(2)
        if name not in seen:
            seen.add(name)
            decls.append((name, "variable"))
    # general fallback for custom/struct/enum/array-typed public vars
    for m in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:\s*\[\s*\d*\s*\])*)\s+public\s+(?:constant\s+)?(\w+)\s*[=;]",
        contract_code,
    ):
        name = m.group(2)
        if name not in seen:
            seen.add(name)
            decls.append((name, "variable"))
    return decls


def _check_getter_completeness(spec: str, contract_code: str) -> list[LintError]:
    """Check 2: every public state var/mapping referenced in spec has a methods-block getter."""
    methods = _extract_methods_block(spec)
    if methods is None:
        return []  # already caught by check 1
    public_decls = _parse_public_declarations(contract_code)
    if not public_decls:
        return []

    # Spec body after methods block
    methods_match = re.search(r'methods\s*\{', spec)
    body_after_methods = spec[methods_match.end():] if methods_match else spec

    errors = []
    for name, kind in public_decls:
        # Is it referenced in any rule/invariant body?
        if not re.search(rf'\b{re.escape(name)}\b', body_after_methods):
            continue
        # Is there a getter declared in methods block?
        if not re.search(rf'function\s+{re.escape(name)}\s*\(', methods):
            errors.append(LintError(
                "missing_getter",
                f"Public {kind} `{name}` is referenced in spec but has no methods-block getter declaration",
            ))
    return errors


def _ghost_names(spec: str) -> set[str]:
    """Names of ghost variables declared in a spec.

    Handles any ghost type (not just `ghost uint256 x;`): mapping ghosts like
    `ghost mapping(address => uint256) mirror_admins;` and array ghosts. The name
    is the last identifier before the `;`/`{` that ends the declaration.
    """
    names: set[str] = set()
    for m in re.finditer(r"\bghost\s+", spec):
        end = spec.find(";", m.end())
        if end == -1:
            end = len(spec)
        tokens = re.findall(r"[A-Za-z_]\w*", spec[m.end():end])
        if tokens:
            names.add(tokens[-1])
    return names


def _check_forbidden_constructs(spec: str) -> list[LintError]:
    """Check 3: reject sum() — CVL has no sum() builtin."""
    errors = []
    for m in re.finditer(r"\bsum\s*\(\s*(\w+)\s*\)", spec):
        target = m.group(1)
        errors.append(LintError(
            "forbidden_sum",
            (
                f"`sum({target})` is invalid — CVL has no `sum()` builtin. "
                "To track an aggregate, declare a `ghost mathint` counter and "
                "update it in a `hook Sstore` block (e.g. increment on true, "
                "decrement on false). Then reference the ghost directly in "
                "the invariant instead of `sum()`."
            ),
        ))
    return errors


def _check_invariant_shape(spec: str) -> list[LintError]:
    """Check 4: invariant body must not contain before/after patterns."""
    errors = []
    # Find invariant blocks
    for m in re.finditer(r'\binvariant\s+(\w+)\s*\([^)]*\)\s*', spec):
        inv_name = m.group(1)
        # Get the body — everything from the match end to the next top-level construct
        rest = spec[m.end():]
        # Simple heuristic: take until next `rule `, `invariant `, or end
        body_match = re.match(r'(.*?)(?=\n(?:rule|invariant)\s|\Z)', rest, re.S)
        body = body_match.group(1) if body_match else rest

        bad_patterns = [
            (r'\b(?:old|@old)\b', "`old`/`@old` (before/after) inside invariant"),
            (r'\bbefore\b', "`before` keyword inside invariant"),
            (r'\bafter\b', "`after` keyword inside invariant"),
        ]
        for pattern, desc in bad_patterns:
            if re.search(pattern, body, re.I):
                errors.append(LintError(
                    "invariant_shape",
                    f"Invariant `{inv_name}` contains {desc} — use a `rule` instead",
                ))
        # if body.strip().endswith(';'):
        #     errors.append(LintError(
        #         "invariant_semicolon",
        #         f"Invariant `{inv_name}` ends with a semicolon `;` which is invalid CVL syntax — remove trailing `;`",
        #     ))
    return errors


def _check_invalid_require(spec: str) -> list[LintError]:
    """Check 8: CVL require statements cannot accept custom error string messages or mismatched parentheses."""
    errors = []
    for i, line in enumerate(spec.splitlines(), 1):
        if re.search(r'\brequire\b.*,\s*"[^"]*"', line):
            errors.append(LintError(
                "invalid_require",
                f"CVL `require` does not support custom error string messages in `{line.strip()}`. Write `require <condition>;` without string message",
                line=i,
            ))
    return errors


def _check_invalid_require_invariant(spec: str) -> list[LintError]:
    """Check 9: requireInvariant must be followed by an invariant call, not an inline expression."""
    errors = []
    for i, line in enumerate(spec.splitlines(), 1):
        if re.search(r'\brequireInvariant\s+.*(?:!=|==|>|<|>=|<=|&&|\|\||\be\.)', line):
            errors.append(LintError(
                "invalid_require_invariant",
                f"`requireInvariant` in `{line.strip()}` must take an invariant function call (e.g. `requireInvariant invName(...)`). For general expressions, use `require` instead",
                line=i,
            ))
    return errors


def _check_bare_solidity_env(spec: str) -> list[LintError]:
    """Check 10: bare block.timestamp, msg.sender, msg.value are invalid in CVL; must use e.block.timestamp, etc."""
    errors = []
    for i, line in enumerate(spec.splitlines(), 1):
        if re.search(r'\b(?<!e\.)(?:block\.timestamp|msg\.sender|msg\.value)\b', line):
            errors.append(LintError(
                "bare_solidity_env",
                f"Bare environment variable in `{line.strip()}` is invalid CVL syntax. Access it via environment struct, e.g. `e.block.timestamp` or `e.msg.sender`",
                line=i,
            ))
    return errors


def _check_undeclared_env(spec: str) -> list[LintError]:
    """Check 5: rules using e.msg / e.block / e.tx without declaring env e."""
    errors = []
    for m in re.finditer(r'\brule\s+(\w+)\s*\(([^)]*)\)\s*(\{.*?\n\})', spec, re.S):
        rule_name = m.group(1)
        params = m.group(2)
        body = m.group(3)
        if re.search(r'\be\.', body) and not (re.search(r'\benv\s+e\b', params) or re.search(r'\benv\s+e\b', body)):
            errors.append(LintError(
                "undeclared_env",
                f"Rule `{rule_name}` references `e.` (e.g. e.msg.sender) but `env e;` is not declared",
            ))
    return errors


def _check_mapping_indexing_syntax(spec: str, contract_code: str) -> list[LintError]:
    """Check 6: public mappings accessed with Solidity array bracket syntax map[key] instead of map(key)."""
    errors = []
    methods_match = re.search(r'methods\s*\{', spec)
    body_after_methods = spec[methods_match.end():] if methods_match else spec
    for m in re.finditer(r'mapping\s*\([^)]+\)\s+public\s+(\w+)', contract_code):
        map_name = m.group(1)
        if re.search(rf'\b{map_name}\s*\[', body_after_methods):
            errors.append(LintError(
                "mapping_syntax",
                f"Public mapping `{map_name}` is accessed as `{map_name}[...]`. In CVL, use function syntax `{map_name}(...)`",
            ))
    return errors


def _check_uncalled_getters(spec: str, contract_code: str) -> list[LintError]:
    """Check 7: 0-arg public getters referenced as variables instead of function calls."""
    errors = []
    methods_match = re.search(r'methods\s*\{', spec)
    body_after_methods = spec[methods_match.end():] if methods_match else spec
    for m in re.finditer(r'(?:uint\d*|int\d*|address|bool|bytes\d*|string)\s+public\s+(\w+)', contract_code):
        var_name = m.group(1)
        if re.search(rf'\b{var_name}\b(?!\s*\()', body_after_methods):
            errors.append(LintError(
                "uncalled_getter",
                f"Public variable getter `{var_name}` is referenced as a variable. In CVL, call it as a function `{var_name}()`",
            ))
    return errors


def _check_invariant_brace_body(spec: str) -> list[LintError]:
    """Check 11: invariant body cannot be a brace block — it is a pure expression.

    LLMs frequently write `invariant foo() { expr; }` copying Solidity function
    syntax. CVL requires `invariant foo() expr;` — no braces around the body.
    """
    errors = []
    for i, line in enumerate(spec.splitlines(), 1):
        if re.search(r'\binvariant\s+\w+\s*\([^)]*\)\s*\{', line):
            errors.append(LintError(
                "invariant_brace_body",
                (
                    f"Invariant in `{line.strip()}` has a brace block `{{...}}`. "
                    "CVL invariant body is a pure expression, not a function body. "
                    "Write `invariant name(params) <expression>;` with no braces around the body."
                ),
                line=i,
            ))
    return errors


def _check_env_in_invariant(spec: str) -> list[LintError]:
    """Check 12: e.msg.* / e.block.* inside invariant bodies are illegal.

    CVL invariants are pure state expressions — they have no env scope.
    `env e` cannot be declared inside them and `e.msg.sender` etc. are
    illegal. Either remove the comparison or rewrite the property as a rule.
    """
    errors = []
    for m in re.finditer(r'\binvariant\s+(\w+)\s*\([^)]*\)\s*', spec):
        inv_name = m.group(1)
        rest = spec[m.end():]
        body_end = re.search(r'\n(?:rule|invariant)\s', rest)
        body = rest[:body_end.start()] if body_end else rest
        if re.search(r'\be\.(?:msg|block|tx)\.', body):
            errors.append(LintError(
                "env_in_invariant",
                (
                    f"Invariant `{inv_name}` references `e.msg.*`/`e.block.*`. "
                    "CVL invariants are pure state expressions with no env scope — "
                    "`env e` cannot be declared inside them. "
                    "Remove the env-dependent comparison or rewrite the property as a `rule`."
                ),
            ))
    return errors


def _check_undeclared_ghost(spec: str) -> list[LintError]:
    """Check 13: ghost-named variables referenced but never declared.

    Catches the pattern where the LLM writes `ghostTotalDeposits` in an
    invariant/rule body without a corresponding `ghost uint256 ghostTotalDeposits;`
    declaration. Heuristic: any identifier starting with `ghost` that is not
    covered by a ghost declaration in the spec.
    """
    declared: set[str] = _ghost_names(spec)
    used: set[str] = set(re.findall(r'\b(ghost\w+)\b', spec))
    return [
        LintError(
            "undeclared_ghost",
            (
                f"Ghost variable `{name}` is used but never declared. "
                f"Add `ghost uint256 {name};` (or the correct type) "
                "before the methods block, and add an `init_state` axiom if needed."
            ),
        )
        for name in sorted(used - declared)
    ]


def _check_sinvoke(spec: str) -> list[LintError]:
    """Check 14: sinvoke/invoke are deprecated in Certora CLI 7+."""
    errors = []
    for i, line in enumerate(spec.splitlines(), 1):
        if re.search(r'\b(sinvoke|invoke)\b', line):
            errors.append(LintError(
                "deprecated_sinvoke",
                f"`sinvoke`/`invoke` in `{line.strip()}` is deprecated. "
                "Call functions directly: `transfer(e, to, amount)` not "
                "`sinvoke transfer(e, to, amount)`",
                line=i,
            ))
    return errors


def _check_envfree_with_env(spec: str) -> list[LintError]:
    """Check 15: functions marked envfree should not receive env argument."""
    methods = _extract_methods_block(spec)
    if not methods:
        return []
    envfree_fns = set(re.findall(r'function\s+(\w+)\s*\([^)]*\)[^;]*\benvfree\b', methods))
    if not envfree_fns:
        return []
    errors = []
    methods_match = re.search(r'methods\s*\{', spec)
    body = spec[methods_match.end():] if methods_match else spec
    for fn_name in sorted(envfree_fns):
        if re.search(rf'\b{fn_name}\s*\(\s*e\s*[,)]', body):
            errors.append(LintError(
                "envfree_with_env",
                f"Function `{fn_name}` is declared `envfree` but called with "
                f"env argument `e`. Remove `e` from the call: `{fn_name}(args)` "
                f"not `{fn_name}(e, args)`",
            ))
    return errors


def _check_require_parens(spec: str) -> list[LintError]:
    """Check 16: CVL require uses `require cond;` not `require(cond);`."""
    errors = []
    for i, line in enumerate(spec.splitlines(), 1):
        # Detect require( — CVL require is a keyword, not a function call.
        # Matches `require(cond);` but not `requireInvariant(...)`.
        if re.search(r'\brequire\s*\(', line) and not re.search(r'\brequireInvariant\b', line):
            errors.append(LintError(
                "require_parens",
                f"CVL `require` in `{line.strip()}` should not use parentheses. "
                "Write `require condition;` not `require(condition);`",
                line=i,
            ))
    return errors


def _check_missing_semicolon(spec: str) -> list[LintError]:
    """Check 17: assert/require statements must end with a semicolon."""
    errors = []
    for i, line in enumerate(spec.splitlines(), 1):
        stripped = line.strip()
        if re.match(r'^(assert|require)\s+', stripped) and not stripped.endswith(';') and not stripped.endswith('{'):
            errors.append(LintError(
                "missing_semicolon",
                f"`{stripped}` is missing a trailing semicolon. "
                "Every `assert` and `require` statement must end with `;`",
                line=i,
            ))
    return errors


def format_lint_errors(errors: list[LintError]) -> str:
    """Format lint errors for injection into repair prompt."""
    return "\n".join(
        f"- [{e.category}] {e.message}" + (f" (line {e.line})" if e.line else "")
        for e in errors
    )
