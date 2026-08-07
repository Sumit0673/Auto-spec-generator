"""
Main specification generator using RAG + LLM.

v2: error-memory persistence, static linter, deterministic methods-block,
    parallel per-function rule drafting.
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from openai import OpenAI
from google import genai

from auto_spec.config import get_config
from auto_spec.cvl_validator import ValidationResult, validate_cvl
from auto_spec.error_memory import ErrorMemory
from auto_spec.lint import lint_spec, format_lint_errors
from auto_spec.methods_block import build_methods_block
from auto_spec.retrieval import contract_profile
from auto_spec.solidity_project import detect_contract_name, load_project
from auto_spec.vector_db import VectorDBManager
from auto_spec.prompts import format_property_gpt_prompt, extract_cvl_spec
from auto_spec.prompts.property_gpt import (
    format_per_function_prompt,
    format_cross_cutting_prompt,
)


class SpecGenerator:
    """Generate CVL specifications for Solidity contracts."""

    def __init__(self, config=None):
        self.config = config or get_config()
        self.vector_db = VectorDBManager(self.config)
        self.last_validation: Optional[ValidationResult] = None
        self.error_memory = ErrorMemory(self.config.ERROR_MEMORY_DB_PATH)
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
        output_path: Optional[str] = None,
        validate: bool = False,
        certora_contract_name: Optional[str] = None,
        validation_timeout: int = 300,
        project_root: Optional[str] = None,
        remappings_file: Optional[str] = None,
        certora_config: Optional[str] = None,
        max_repairs: int = 10,
        parallel: bool = True,
    ) -> str:
        """
        Generate CVL specification for a Solidity contract.

        Args:
            contract_path: Path to Solidity contract file
            query: Search query (defaults to contract source)
            top_k: Number of reference specs to retrieve
            output_path: Path to save generated spec file
            parallel: Use parallel per-function drafting (Phase 5)

        Returns:
            str: Generated CVL specification
        """
        project = load_project(contract_path, project_root, remappings_file)
        contract_path = project.entrypoint
        contract_code = project.source_text
        contract_name = certora_contract_name or detect_contract_name(
            contract_path.read_text(encoding="utf-8", errors="replace"), contract_path.stem
        )
        if project.unresolved_imports:
            print("Warning: unresolved imports: " + ", ".join(project.unresolved_imports))

        # Contract fingerprint for retrieval + error memory
        query = query or contract_profile(contract_code, contract_name)
        contract_hash = ErrorMemory.hash_contract(contract_code)
        run_id = ErrorMemory.new_run_id()

        print(f"Query: {query}")

        # Fetch known errors from prior runs
        known_errors = self.error_memory.all_known_errors(contract_hash)
        if known_errors:
            print(f"📝 Loaded {len(known_errors)} known-bad patterns from prior runs")

        # Retrieve reference specs
        print(f"Retrieving similar specs for: {contract_name}...")
        retrieved_context = self.vector_db.query(query, top_k=top_k)

        if not retrieved_context:
            print("Warning: No similar specs found in database (or all below similarity floor)")
        else:
            print(f"Found {len(retrieved_context)} reference specs")
            for i, ctx in enumerate(retrieved_context):
                print(f"  [{i}] {getattr(ctx, 'name', ctx)!r} score={getattr(ctx, 'score', '?')}")

        # Build deterministic methods block (Phase 4)
        det_methods_block = build_methods_block(contract_code)
        print(f"Built deterministic methods block ({det_methods_block.count(chr(10))} entries)")

        # Generate spec using LLM
        print(f"Generating CVL spec using {self.config.LLM_PROVIDER}/{self.config.LLM_MODEL}...")
        if parallel:
            spec_content = self._parallel_generate(
                contract_code=contract_code,
                retrieved_context=retrieved_context,
                contract_name=contract_name,
                methods_block=det_methods_block,
                known_errors=known_errors,
            )
        else:
            spec_content = self._call_llm(
                contract_code=contract_code,
                retrieved_context=retrieved_context,
                contract_name=contract_name,
                known_errors=known_errors,
            )

        spec_content = _clean_cvl_spec(spec_content, det_methods_block, contract_code)

        validation_output_path = self.save_spec(spec_content, output_path, 0)

        if validate:
            for attempt in range(max_repairs):
                # Phase 2: run static linter before Certora
                lint_errors = lint_spec(spec_content, contract_code)
                if lint_errors:
                    print(f"🔍 Linter caught {len(lint_errors)} issues (skipping Certora)")
                    feedback_lines = [e.message for e in lint_errors]
                    error_source = "lint"
                else:
                    # Linter passed — run Certora
                    result = validate_cvl(
                        contract_path, validation_output_path, contract_name,
                        validation_timeout, project.root, certora_config
                    )
                    if result.passed:
                        print(f"✅ Certora compilation passed on attempt {attempt + 1}")
                        self.error_memory.record(contract_hash, run_id, attempt, spec_content, [])
                        break
                    feedback_lines = _enrich_certora_feedback(result.output, spec_content)
                    error_source = "certora"

                # Record errors
                self.error_memory.record(contract_hash, run_id, attempt, spec_content, feedback_lines)
                known_errors = self.error_memory.all_known_errors(contract_hash)

                print(f"Static check failed (attempt {attempt+1}, source={error_source})")
                spec_content = self._call_llm(
                    contract_code=contract_code,
                    retrieved_context=retrieved_context,
                    contract_name=contract_name,
                    repair_feedback=feedback_lines,
                    previous_spec=spec_content,
                    known_errors=known_errors,
                )
                spec_content = _clean_cvl_spec(spec_content, det_methods_block, contract_code)
                validation_output_path = self.save_spec(spec_content, output_path, attempt + 1)

                print("#" * 50)
                print(f"Attempt {attempt+1} ({error_source}) failures:")
                for err in feedback_lines[:5]:
                    print(f"  • {err}")
                print("#" * 50)
            else:
                # Loop exhausted without passing — still record final state
                self.error_memory.record(contract_hash, run_id, max_repairs, spec_content, feedback_lines)
        else:
            # No validation — still record for future runs
            self.error_memory.record(contract_hash, run_id, 0, spec_content, [])

        return spec_content

    def save_spec(self, spec_content: str, output_path: Optional[str], attempt: int = 0) -> Path:
        """Save the generated CVL spec to a file."""
        if not output_path:
            target = self.config.OUTPUT_DIR / f"generated_spec_attempt{attempt}.spec"
        else:
            p = Path(output_path)
            suffix = p.suffix if p.suffix in [".spec", ".cvl"] else ".spec"
            target = p.parent / f"{p.stem}_attempt{attempt}{suffix}"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(spec_content)
        print(f"✓ CVL spec saved to: {target}")
        return target

    # ------------------------------------------------------------------
    # Phase 5: parallel per-function rule drafting + merge
    # ------------------------------------------------------------------

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
            return self._call_llm(
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
            raw = self._raw_llm_call(system_prompt, user_prompt)
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
            raw = self._raw_llm_call(system_prompt, user_prompt)
            return "__cross_cutting__", extract_cvl_spec(raw) if raw else ""

        with ThreadPoolExecutor(max_workers=min(len(func_sigs) + 1, 8)) as executor:
            futures = [executor.submit(_draft_function, fn) for fn in func_sigs]
            futures.append(executor.submit(_draft_cross_cutting))

            for future in as_completed(futures):
                key, spec_fragment = future.result()
                results[key] = spec_fragment
                print(f"  ✓ Drafted: {key}")

        return self._merge_specs(methods_block, results, func_sigs)

    @staticmethod
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

        return methods_block + "\n\n" + "\n\n".join(rule_blocks)

    # ------------------------------------------------------------------
    # LLM call layer
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        contract_code: str,
        retrieved_context: list,
        contract_name: str,
        repair_feedback: Optional[list[str]] = None,
        previous_spec: Optional[str] = None,
        known_errors: list[str] | None = None,
    ) -> str:
        system_prompt, user_prompt = format_property_gpt_prompt(
            contract_code=contract_code,
            retrieved_context=retrieved_context,
            contract_name=contract_name
        )
        system_prompt = f"{system_prompt}"
        temperature = self.config.LLM_TEMPERATURE

        # Inject known-bad patterns from error memory (Phase 1)
        if known_errors:
            error_list = "\n".join(f"- {e}" for e in known_errors[:20])
            user_prompt += (
                "\n\n### KNOWN-BAD PATTERNS FOR THIS CONTRACT (from prior runs) — do not repeat these:\n"
                f"{error_list}\n"
            )

        if repair_feedback:
            feedback_block = "\n".join(f"- {issue}" for issue in repair_feedback)
            if previous_spec:
                user_prompt = f"""{user_prompt}

                ### PREVIOUS SPEC (has bugs — you must EDIT this, not regenerate from context)

                ```cvl
                {previous_spec}
                ```

                ### PROBLEMS FOUND IN THE ABOVE SPEC
                {feedback_block}

                ### TASK
                Return the FULL corrected spec. Make the MINIMAL edit needed to fix each
                problem listed above. Do not rewrite rules or invariants that were not
                flagged. Do not reintroduce any previously-fixed issue. Preserve every
                correct construct from the previous spec exactly as-is.
                """
            else:
                user_prompt += (
                    "\n\nThe previous spec you generated failed with these problems:\n"
                    f"{feedback_block}\n\nRegenerate the FULL spec, fixing every issue listed."
                )
            temperature = 0

        raw_response = self._raw_llm_call(system_prompt, user_prompt, temperature)
        spec_content = extract_cvl_spec(raw_response)
        if not spec_content:
            raise RuntimeError(
                "LLM response did not contain recognizable CVL; no spec was saved. "
                "Adjust the prompt or retry with different retrieved context."
            )
        return spec_content

    def _raw_llm_call(self, system_prompt: str, user_prompt: str, temperature: float | None = None) -> str:
        """Send a single prompt pair to the configured LLM provider. Returns raw text.

        Provider routing is driven entirely by config — no hardcoded URLs here.
        """
        if temperature is None:
            temperature = self.config.LLM_TEMPERATURE
        try:
            if self.config.is_gemini:
                client = genai.Client()
                response = client.models.generate_content(
                    model=self.config.LLM_MODEL,
                    contents=user_prompt,
                    config={
                        "system_instruction": system_prompt,
                        "temperature": temperature,
                        "max_output_tokens": self.config.LLM_MAX_TOKENS,
                    },
                )
                return response.text.strip()

            # All other providers are OpenAI-compatible
            client = OpenAI(
                base_url=self.config.LLM_BASE_URL,  # None → default api.openai.com
                api_key=self.config.LLM_API_KEY,
            )
            max_tokens = self.config.LLM_MAX_TOKENS
            response = client.chat.completions.create(
                model=self.config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content.strip()

        except Exception as e:
            raise RuntimeError(f"Error calling LLM: {e}")



def _clean_cvl_spec(spec_content: str, det_methods_block: str, contract_code: str = "") -> str:
    """Auto-repair common structural and syntax errors in generated CVL specs."""
    # 1. Strip mutability keywords from methods block
    spec_content = _strip_solidity_mutability_keywords(spec_content)

    # # 2. Fix trailing semicolons on invariant definitions
    # spec_content = re.sub(r'(\binvariant\s+\w+\s*\([^)]*\)\s*[^;{\n]+);', r'\1', spec_content)

    # 3. Replace methods block with deterministic block
    spec_content = _replace_methods_block(spec_content, det_methods_block)

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
    def _fix_rule_env(match: re.Match) -> str:
        rule_head = match.group(1)
        rule_body = match.group(2)
        if re.search(r'\be\.', rule_body) and not re.search(r'\b(env\s+e\b|\(env\s+e\b|,\s*env\s+e\b)', rule_head + rule_body):
            return f"{rule_head} {{\n    env e;\n{rule_body[1:]}"
        return match.group(0)

    spec_content = re.sub(r'(\brule\s+\w+\s*\([^)]*\))\s*(\{.*?\n\})', _fix_rule_env, spec_content, flags=re.S)

    # 7. Auto-fix invalid require statements with string error messages
    spec_content = re.sub(r'\brequire\s*\(\s*([^,;]+?)\s*,\s*"[^"]*"\s*\)\s*;', r'require \1;', spec_content)
    spec_content = re.sub(r'\brequire\s+([^,;]+?)\s*,\s*"[^"]*"\s*\)\s*;', r'require \1;', spec_content)
    spec_content = re.sub(r'\brequire\s+([^,;]+?)\s*,\s*"[^"]*"\s*;', r'require \1;', spec_content)

    # 8. Convert requireInvariant <expression>; -> require <expression>;
    spec_content = re.sub(r'\brequireInvariant\s+([^;()]*?(?:!=|==|>|<|>=|<=|&&|\|\||\be\.)[^;]*);', r'require \1;', spec_content)

    # 9. Convert bare block.timestamp / msg.sender / msg.value -> e.block.timestamp / e.msg.sender / e.msg.value
    #    Skipped inside invariant bodies — CVL invariants have no env scope,
    #    so emitting e.* there produces "Variable `e` has not been declared".
    #    The linter (check 10) will catch bare refs that remain and surface
    #    them accurately in the repair prompt.
    spec_content = _apply_env_subs_outside_invariants(spec_content)

    return spec_content


def _enrich_certora_feedback(output: str, spec_content: str) -> list[str]:
    """Extract Certora error messages and attach corresponding code line snippets."""
    spec_lines = spec_content.splitlines()
    enriched = []

    for line in output.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        match = re.search(r'Error in spec file \([^:]+:(\d+)(?::\d+)?\):\s*(.*)', line_str)
        if match:
            line_num = int(match.group(1))
            err_msg = match.group(2)
            code_snippet = spec_lines[line_num - 1] if 0 < line_num <= len(spec_lines) else ""
            enriched.append(f"Line {line_num} `{code_snippet.strip()}` -> Error: {err_msg}")
        elif "Syntax error:" in line_str or "TypeError:" in line_str or "Undefined" in line_str:
            enriched.append(line_str)
        elif "Variable `" in line_str and "has not been declared" in line_str:
            enriched.append(line_str)

    return enriched if enriched else [l for l in output.splitlines() if l.strip() and not l.startswith("WARNING") and not l.startswith("Compiling")]


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
                return deterministic_block + spec[i + 1:]
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


def main() -> None:
    """CLI entrypoint for generating a CVL spec from a Solidity contract."""
    parser = argparse.ArgumentParser(description="Generate a CVL specification for a Solidity contract")
    parser.add_argument("contract", help="Path to the Solidity contract file")
    parser.add_argument("--query", "-q", default=None, help="Search query to use for retrieving reference specs")
    parser.add_argument("--top_k", type=int, default=None, help="Number of reference specs to retrieve")
    parser.add_argument("--output", "-o", default=None, help="Path to save the generated .spec file")
    parser.add_argument("--quiet", action="store_true", help="Do not print the generated spec to stdout")
    parser.add_argument("--check", action="store_true", help="Compile the generated CVL with Certora before returning")
    parser.add_argument("--contract-name", default=None, help="Contract name passed to Certora (defaults to the file stem)")
    parser.add_argument("--validation-timeout", type=int, default=300, help="Certora compilation timeout in seconds")
    parser.add_argument("--project-root", default=None, help="Solidity project root (defaults to the contract directory)")
    parser.add_argument("--remappings", default=None, help="Foundry remappings.txt file")
    parser.add_argument("--certora-config", default=None, help="Existing Certora .conf/.json input for --check")
    parser.add_argument("--no-parallel", action="store_true", help="Disable parallel per-function drafting")
    args = parser.parse_args()

    generator = SpecGenerator()
    spec = generator.generate(
        contract_path=args.contract,
        query=args.query,
        top_k=args.top_k,
        output_path=args.output,
        validate=args.check,
        certora_contract_name=args.contract_name,
        validation_timeout=args.validation_timeout,
        project_root=args.project_root,
        remappings_file=args.remappings,
        certora_config=args.certora_config,
        parallel=not args.no_parallel,
    )

    if not args.quiet:
        print(spec)


if __name__ == "__main__":
    main()
