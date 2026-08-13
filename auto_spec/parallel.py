import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from auto_spec.call_llm import _call_llm, _raw_llm_call
from auto_spec.prompts import format_per_function_prompt, format_cross_cutting_prompt, extract_cvl_spec



def _parallel_generate(
        self,
        contract_code: str,
        retrieved_context: list,
        contract_name: str,
        methods_block: str,
        known_errors: list[str] | None = None,
    ) -> str:
        """Draft rules in parallel per-function, plus one cross-cutting call."""
        # Extract function signatures
        func_sigs = re.findall(
            r'function\s+(\w+)\s*\([^)]*\)\s+(?:external|public)',
            contract_code,
        )
        if not func_sigs:
            # Fallback to single-call if no functions found
            return _call_llm(
                self,
                contract_code=contract_code,
                retrieved_context=retrieved_context,
                contract_name=contract_name,
                known_errors=known_errors,
            )

        print(f"⚡ Parallel drafting: {len(func_sigs)} function calls + 1 cross-cutting call")
        results: dict[str, str] = {}

        def _draft_function(func_name: str) -> tuple[str, str]:
            system_prompt, user_prompt = format_per_function_prompt(
                contract_code=contract_code,
                function_name=func_name,
                methods_block=methods_block,
                retrieved_context=retrieved_context,
                contract_name=contract_name,
                known_errors=known_errors,
            )
            raw = _raw_llm_call(self, system_prompt, user_prompt)
            return func_name, extract_cvl_spec(raw) if raw else ""

        def _draft_cross_cutting() -> tuple[str, str]:
            system_prompt, user_prompt = format_cross_cutting_prompt(
                contract_code=contract_code,
                function_names=func_sigs,
                methods_block=methods_block,
                retrieved_context=retrieved_context,
                contract_name=contract_name,
                known_errors=known_errors,
            )
            raw = _raw_llm_call(self, system_prompt, user_prompt)
            return "__cross_cutting__", extract_cvl_spec(raw) if raw else ""

        with ThreadPoolExecutor(max_workers=min(len(func_sigs) + 1, 8)) as executor:
            futures = [executor.submit(_draft_function, fn) for fn in func_sigs]
            futures.append(executor.submit(_draft_cross_cutting))

            for future in as_completed(futures):
                key, spec_fragment = future.result()
                results[key] = spec_fragment
                print(f"  ✓ Drafted: {key}")

        return _merge_specs(methods_block, results, func_sigs)

def _merge_specs(methods_block: str, fragments: dict[str, str], func_order: list[str]) -> str:
    """Merge per-function rule fragments + cross-cutting into one spec.

    Pure code, no LLM call. Deduplicates by rule/invariant name.
    """
    seen_names: set[str] = set()
    rule_blocks: list[str] = []

    def _extract_named_blocks(text: str) -> list[tuple[str, str]]:
        """Extract (name, full_block) for each rule/invariant in text."""
        blocks = []
        # Split on top-level rule/invariant declarations
        parts = re.split(r'(?=\b(?:rule|invariant)\s+\w+)', text)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            name_match = re.match(r'(rule|invariant)\s+(\w+)', part)
            if name_match:
                blocks.append((name_match.group(2), part))
        return blocks

    # Per-function rules first (in function order)
    for fn in func_order:
        fragment = fragments.get(fn, "")
        # Strip any methods block the LLM might have emitted
        fragment = re.sub(r'methods\s*\{[^}]*\}', '', fragment, flags=re.S).strip()
        for name, block in _extract_named_blocks(fragment):
            if name not in seen_names:
                seen_names.add(name)
                rule_blocks.append(block)

    # Cross-cutting invariants/rules
    cross = fragments.get("__cross_cutting__", "")
    cross = re.sub(r'methods\s*\{[^}]*\}', '', cross, flags=re.S).strip()
    for name, block in _extract_named_blocks(cross):
        if name not in seen_names:
            seen_names.add(name)
            rule_blocks.append(block)

    # Preserve ghost/hook/definition/using/import/init_state declarations the
    # drafters emitted before their rules. Dropping them left `sum(ghostMapping)`
    # patterns undeclared, so the linter re-flagged the same error every round.
    top_decls: list[str] = []
    seen_decls: set[str] = set()
    for fragment in fragments.values():
        for decl in _extract_top_level_decls(fragment):
            key = decl.split("{")[0].strip()
            if key and key not in seen_decls:
                seen_decls.add(key)
                top_decls.append(decl)

    body = "\n\n".join(rule_blocks)
    if top_decls:
        body = "\n\n".join(top_decls) + "\n\n" + body
    return methods_block + "\n\n" + body

def _extract_top_level_decls(text: str) -> list[str]:
    """Capture ghost/hook/definition/using/import/init_state declarations emitted
    before the first rule/invariant in a draft fragment.

    The parallel merge used to drop these, which left `sum(ghostMapping)` style
    patterns with their ghost declaration missing — so the linter flagged the
    (valid) pattern on every single repair round.
    """
    parts = re.split(r"(?=\b(?:rule|invariant)\s+\w+)", text)
    preamble = parts[0] if parts else ""
    body = re.sub(r"/\*.*?\*/|//.*", "", preamble, flags=re.S | re.M)
    decls: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b(ghost|hook|definition|using|import|init_state)\b", body):
        j = m.end()
        depth = 0
        saw_structure = False
        while j < len(body):
            ch = body[j]
            if ch == "{":
                depth += 1
                saw_structure = True
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            elif ch == ";" and depth == 0:
                j += 1
                break
            elif ch == "\n" and depth == 0 and not saw_structure:
                # A keyword not followed by `{` or `;` on its line is prose
                # ("we use ghost variables to track..."), not a declaration.
                break
            j += 1
        decl = body[m.start():j].strip()
        # A real CVL declaration is a statement (`...;`) or a braced block
        # (`...{...}`); a bare prose line ("we use ghost variables...") is not.
        if not decl or (not decl.endswith(";") and "{" not in decl):
            continue
        key = decl.split("{")[0].split(";")[0].strip()
        if key and key not in seen:
            seen.add(key)
            decls.append(decl)
    return decls