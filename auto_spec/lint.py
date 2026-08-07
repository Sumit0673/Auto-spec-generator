"""Static CVL linter — catches known error classes without calling Certora.

Runs after every LLM generation, before certoraRun. If it fails, the repair
loop uses lint findings as feedback instead of burning a Certora round trip.
"""

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


def _parse_public_declarations(contract_code: str) -> list[tuple[str, str]]:
    """Parse public state vars and mappings from Solidity source.

    Returns list of (name, kind) where kind is 'variable' or 'mapping'.
    """
    decls: list[tuple[str, str]] = []
    # public mappings: mapping(X => Y) public name;
    for m in re.finditer(r'mapping\s*\([^)]+\)\s+public\s+(\w+)', contract_code):
        decls.append((m.group(1), "mapping"))
    # public variables: type public name;
    for m in re.finditer(
        r'(?:uint\d*|int\d*|address|bool|bytes\d*|string)\s+public\s+(\w+)',
        contract_code,
    ):
        decls.append((m.group(1), "variable"))
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


def _check_forbidden_constructs(spec: str) -> list[LintError]:
    """Check 3: reject sum() over non-ghost identifiers."""
    errors = []
    # Find ghost declarations
    ghosts = set(re.findall(r'ghost\s+\w+\s+(\w+)', spec))
    # Find sum( usage
    for m in re.finditer(r'\bsum\s*\(\s*(\w+)', spec):
        target = m.group(1)
        if target not in ghosts:
            errors.append(LintError(
                "forbidden_sum",
                f"`sum({target})` — sum() can only be applied to ghost variables, not `{target}`",
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
    declared: set[str] = set(re.findall(r'\bghost\s+\S+\s+(\w+)\s*[;{]', spec))
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


def format_lint_errors(errors: list[LintError]) -> str:
    """Format lint errors for injection into repair prompt."""
    return "\n".join(
        f"- [{e.category}] {e.message}" + (f" (line {e.line})" if e.line else "")
        for e in errors
    )
