import re

def _clean_cvl_spec(spec_content: str, det_methods_block: str, contract_code: str = "") -> str:
    """Auto-repair common structural and syntax errors in generated CVL specs."""
    # 0a. Strip deprecated sinvoke/invoke keywords
    spec_content = re.sub(r'\bsinvoke\s+', '', spec_content)
    spec_content = re.sub(r'\binvoke\s+', '', spec_content)

    # # 0b. Fix require(cond); -> require cond;
    # spec_content = re.sub(r'\brequire\s*\(([^)]+)\)\s*;', r'require \1;', spec_content)

    # 1. Strip mutability keywords from methods block
    spec_content = _strip_solidity_mutability_keywords(spec_content)

    # # 2. Fix trailing semicolons on invariant definitions
    # spec_content = re.sub(r'(\binvariant\s+\w+\s*\([^)]*\)\s*[^;{\n]+);', r'\1', spec_content)

    # 3. Replace methods block with deterministic block
    spec_content = _replace_methods_block(spec_content, det_methods_block)  #CHECK Why are we doin this??

    # 4. Auto-fix mapping getter index syntax: map[x] -> map(x)
    if contract_code:
        for m in re.finditer(r'mapping\s*\([^)]+\)\s+public\s+(\w+)', contract_code):
            map_name = m.group(1)
            spec_content = re.sub(rf'\b{map_name}\s*\[([^\]]+)\]', rf'{map_name}(\1)', spec_content)

        # 5. Auto-fix 0-arg getter variables missing parentheses: e.g. > unlockTime; -> > unlockTime();
        for m in re.finditer(r'(?:uint\d*|int\d*|address|bool|bytes\d*|string)\s+public\s+(\w+)', contract_code):
            var_name = m.group(1)
            methods_match = re.search(r'methods\s*\{[^}]*\}', spec_content, re.S)
            if methods_match:
                head = spec_content[:methods_match.end()]
                tail = spec_content[methods_match.end():]
                tail = re.sub(rf'\b{var_name}\b(?!\s*\()', f'{var_name}()', tail)
                spec_content = head + tail

    # 6. Auto-inject missing 'env e;' in rules referencing e.
    def _fix_rule_env(match: re.Match[str]) -> str:
        rule_head = match.group(1)
        rule_body = match.group(2)
        if re.search(r'\be\.', rule_body) and not re.search(r'\b(env\s+e\b|\(env\s+e\b|,\s*env\s+e\b)', rule_head + rule_body):
            return f"{rule_head} {{\n    env e;\n{rule_body[1:]}"
        return match.group(0)

    spec_content = re.sub(r'(\brule\s+\w+\s*\([^)]*\))\s*(\{.*?\n\})', _fix_rule_env, spec_content, flags=re.S)

    # # 7. Auto-fix invalid require statements with string error messages
    # spec_content = re.sub(r'\brequire\s*\(\s*([^,;]+?)\s*,\s*"[^"]*"\s*\)\s*;', r'require \1;', spec_content)
    # spec_content = re.sub(r'\brequire\s+([^,;]+?)\s*,\s*"[^"]*"\s*\)\s*;', r'require \1;', spec_content)
    # spec_content = re.sub(r'\brequire\s+([^,;]+?)\s*,\s*"[^"]*"\s*;', r'require \1;', spec_content)

    # 8. Convert requireInvariant <expression>; -> require <expression>;
    spec_content = re.sub(r'\brequireInvariant\s+([^;()]*?(?:!=|==|>|<|>=|<=|&&|\|\||\be\.)[^;]*);', r'require \1;', spec_content)

    # 9. Convert bare block.timestamp / msg.sender / msg.value -> e.block.timestamp / e.msg.sender / e.msg.value
    #    Skipped inside invariant bodies — CVL invariants have no env scope,
    #    so emitting e.* there produces "Variable `e` has not been declared".
    #    The linter (check 10) will catch bare refs that remain and surface
    #    them accurately in the repair prompt.
    spec_content = _apply_env_subs_outside_invariants(spec_content)

    # 10. Auto-inject missing ghost declarations.
    #     LLMs frequently reference ghostXxx identifiers without declaring them.
    #     Asking via repair prompt doesn't work reliably — just inject deterministically.
    spec_content = _inject_ghost_declarations(spec_content)

    # 11. Strip env arg from envfree function calls
    spec_content = _strip_env_from_envfree(spec_content)

    # 12. Append missing semicolons on assert/require statements
    # spec_content = re.sub(
    #     r'^(\s*(?:assert)\s+.+[^;{}\s])\s*$',
    #     r'\1;',
    #     spec_content,
    #     flags=re.MULTILINE,
    # )

    # 13. Canonicalize legacy hook syntax LAST — after step 4 rewrote rule-body
    #     `map[x]` → `map(x)`, restore the bracket KEY form the parser requires:
    #     `hook Sstore admins(KEY address account) ...` → `admins[KEY address account]`,
    #     and drop the redundant ` STORAGE` keyword.
    spec_content, _ = _canonicalize_cvl_syntax(spec_content)

    # 14. CVL has no `address(0)`; the zero-address literal is `0x0`.
    spec_content = re.sub(r"\baddress\s*\(\s*0\s*\)", "0x0", spec_content)

    # 15. Inject `env e` into calls to non-envfree methods inside rules that
    #     declare `env e;` (parser rejects env-less calls).
    spec_content = _inject_missing_env(spec_content, det_methods_block)

    # 16. Fix invariant syntax: strip illegal { } wrapping and ensure trailing ;
    spec_content = _fix_invariant_syntax(spec_content)

    return spec_content

def _inject_missing_env(spec: str, det_methods_block: str) -> str:
    """Inject `env e` as the first argument of calls to NON-envfree methods,
    inside rules that declare `env e;`.

    CVL requires an env argument for non-envfree methods; LLMs routinely omit it
    and certoraRun rejects the call ("Missing environment parameter"). This is
    mechanical and unambiguous, so it is fixed deterministically instead of being
    sent back to the LLM. Calls that already pass an env, and all envfree calls,
    are left untouched.
    """
    envfree = set(
        re.findall(r"function\s+(\w+)\s*\([^)]*\)\s+external[^;]*?\benvfree", det_methods_block)
    )
    declared = set(re.findall(r"function\s+(\w+)\s*\(", det_methods_block))
    non_envfree = declared - envfree
    if not non_envfree:
        return spec

    def _fix_call(m: re.Match[str]) -> str:
        fn, args = m.group(1), m.group(2)
        if fn not in non_envfree:
            return m.group(0)
        stripped = args.strip()
        if stripped == "":
            return f"{fn}(e)"
        if stripped.startswith("e,") or stripped == "e" or stripped.startswith("env "):
            return m.group(0)  # already has an env argument
        return f"{fn}(e, {stripped})"

    def _fix_rule(m: re.Match[str]) -> str:
        head, body = m.group(1), m.group(2)
        if not re.search(r"\benv\s+e\s*;", body):
            return m.group(0)
        return head + re.sub(r"\b(\w+)\s*\(([^()]*)\)", _fix_call, body)

    return re.sub(r"(\brule\s+\w+\s*\([^)]*\))\s*(\{.*?\n\})", _fix_rule, spec, flags=re.S)

def _strip_solidity_mutability_keywords(spec_content: str) -> str:
    """Best-effort auto-repair: strip Solidity mutability keywords from methods
    block entries instead of relying on the LLM to remove them correctly."""
    return re.sub(r'\b(payable|view|pure|nonpayable)\b\s*', '', spec_content)


def _replace_methods_block(spec: str, deterministic_block: str) -> str:
    """Replace whatever methods block the LLM emitted with the deterministic one."""
    match = re.search(r'methods\s*\{', spec)
    if not match:
        # No methods block — prepend it
        return deterministic_block + "\n\n" + spec
    # Find matching closing brace
    depth, start = 1, match.end()
    for i in range(start, len(spec)):
        if spec[i] == '{':
            depth += 1
        elif spec[i] == '}':
            depth -= 1
            if depth == 0:
                return spec[:match.start()] + deterministic_block + spec[i + 1:]
    # Couldn't find closing brace — prepend
    return deterministic_block + "\n\n" + spec

def _apply_env_subs_outside_invariants(spec: str) -> str:
    """Apply bare-env substitutions only outside invariant bodies.

    CVL invariants are pure state expressions with no env scope.
    Emitting e.block.timestamp / e.msg.sender inside them causes
    'Variable `e` has not been declared'. We leave bare refs alone there
    so linter check 10 catches them and feeds accurate feedback to the
    repair prompt instead of silently producing illegal syntax.

    ponytail: splits on \\n(rule|invariant) boundaries; may miss
    preserved/filtered sub-blocks nested inside invariants.
    """
    def _apply(text: str) -> str:
        text = re.sub(r'\b(?<!e\.)block\.timestamp\b', 'e.block.timestamp', text)
        text = re.sub(r'\b(?<!e\.)msg\.sender\b', 'e.msg.sender', text)
        text = re.sub(r'\b(?<!e\.)msg\.value\b', 'e.msg.value', text)
        return text

    # Lookahead keeps the boundary token in the next chunk.
    parts = re.split(r'(?=\n(?:rule|invariant)\s)', spec)
    return ''.join(
        part if re.match(r'\ninvariant\s', part) else _apply(part)
        for part in parts
    )


def _inject_ghost_declarations(spec: str) -> str:
    """Auto-inject ghost variable declarations that are referenced but never declared.

    LLMs reliably use `ghostXxx` naming convention but unreliably write the
    corresponding `ghost uint256 ghostXxx;` declaration. Injecting it here is
    cheaper and more reliable than burning repair-loop round trips on it.
    Declarations are inserted immediately before the methods block (or at the
    top if no methods block exists). Default type is uint256.
    """
    declared: set[str] = set(re.findall(r'\bghost\s+\S+\s+(\w+)\s*[;{]', spec))
    used: set[str] = set(re.findall(r'\b(ghost\w+)\b', spec))
    missing = sorted(used - declared)
    if not missing:
        return spec
    decls = '\n'.join(f'ghost uint256 {name};' for name in missing) + '\n\n'
    methods_match = re.search(r'\bmethods\s*\{', spec)
    if methods_match:
        pos = methods_match.start()
        return spec[:pos] + decls + spec[pos:]
    return decls + spec

def _strip_env_from_envfree(spec: str) -> str:
    """Remove env arg from calls to functions declared envfree in methods{}."""
    methods_match = re.search(r'methods\s*\{', spec)
    if not methods_match:
        return spec
    # Find closing brace
    depth, start = 1, methods_match.end()
    methods_end = len(spec)
    for i in range(start, len(spec)):
        if spec[i] == '{':
            depth += 1
        elif spec[i] == '}':
            depth -= 1
            if depth == 0:
                methods_end = i
                break
    methods_content = spec[start:methods_end]
    envfree_fns = set(re.findall(r'function\s+(\w+)\s*\([^)]*\)[^;]*\benvfree\b', methods_content))
    if not envfree_fns:
        return spec
    head = spec[:methods_end + 1]
    tail = spec[methods_end + 1:]
    for fn_name in envfree_fns:
        # fn(e, args...) -> fn(args...)
        tail = re.sub(rf'\b{fn_name}\s*\(\s*e\s*,\s*', f'{fn_name}(', tail)
        # fn(e) -> fn()
        tail = re.sub(rf'\b{fn_name}\s*\(\s*e\s*\)', f'{fn_name}()', tail)
    return head + tail

# ── Legacy CVL hook/ghost syntax canonicalization ─────────────────────────────
# The reference corpus (and therefore the LLM) uses older hook forms that the
# installed Certora parser rejects. These are deterministic rewrites — the LLM
# can never fix what its retrieved examples teach it, so the tool must.

# 1. `hook Sstore admins(KEY address account) ...` → `admins[KEY address account]`
#    (paren-key is legacy; the parser requires brackets).
#    Handles both structures: `Sstore <getter>(KEY ...)` and the Sload form
#    `Sload <type> <name> <getter>(KEY ...)`.
_HOOK_KEY_PAREN = re.compile(
    r"\bhook\s+(Sstore|Sload)\s+"
    r"((?:[A-Za-z_]\w*\s+)*?)"
    r"([A-Za-z_]\w*(?:\s*\[[^\]]*\]\s*)*(?:\.[A-Za-z_]\w*)*)\s*\(\s*(KEY[^()]*)\)"
)

# 2. `hook Sstore x uint v STORAGE {` → `hook Sstore x uint v {` (redundant keyword).
_HOOK_TRAILING_STORAGE = re.compile(r"\b(hook\s+(?:Sstore|Sload)\s+[^\n{]*?)\s+STORAGE\s*(\{)")

def _canonicalize_cvl_syntax(spec: str) -> tuple[str, bool]:
    """Convert legacy CVL hook syntax to the form the installed parser accepts.

    Idempotent and purely syntactic — never changes semantics. Returns
    (canonical_spec, changed).
    """
    original = spec
    spec = _HOOK_KEY_PAREN.sub(
        lambda m: f"hook {m.group(1)} {m.group(2)}{m.group(3)}[{m.group(4).strip()}]",
        spec,
    )
    spec = _HOOK_TRAILING_STORAGE.sub(r"\1 \2", spec)
    return (spec, spec != original)


def _fix_invariant_syntax(spec: str) -> str:
    """Fix invariant bodies: strip illegal { } wrapping and ensure trailing `;` (CVL 2)."""
    # 1. Strip brace-wrapping:  invariant name(params) { body } -> invariant name(params)\n    body;
    def _unwrap(m: re.Match[str]) -> str:
        header = m.group(1)
        body = m.group(2).strip().rstrip(';')
        return f"{header}\n    {body};"
    spec = re.sub(r'(invariant\s+\w+\s*\([^)]*\))\s*\{([^}]+)\}', _unwrap, spec)

    # 2. Add missing semicolon on invariant body line
    spec = re.sub(
        r'(invariant\s+\w+\s*\([^)]*\)\s*\n[ \t]+[^\n]+[^;\s\n])([ \t]*\n)',
        r'\1;\2',
        spec,
    )
    return spec