import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


from auto_spec.config import get_config
from auto_spec.cvl_validator import ValidationResult, validate_cvl
from auto_spec.error_memory import ErrorMemory, normalize_error
from auto_spec.lint import lint_spec, format_lint_errors
from auto_spec.methods_block import build_methods_block
from auto_spec.retrieval import contract_profile
from auto_spec.solidity_project import detect_contract_name, load_project
from auto_spec.vector_db import VectorDBManager

from auto_spec.parallel import _parallel_generate
from auto_spec.call_llm import _call_llm
from auto_spec.repair import _clean_cvl_spec


def _exit_code_for_status(status: str) -> int:
    if status in ("passed", "skipped"):
        return 0
    if status == "unverified":
        return 2
    return 1  # "failed" and any unknown status


def _normalized_error_keys(feedback_lines: list[str]) -> frozenset[str]:
    """Canonical, deduplicated set of normalized error keys for the current iteration.

    Used for consecutive-recurrence detection: the same key in iteration N and
    N+1 means the error survived a repair round unfixed.
    """
    return frozenset(
        key
        for key in (normalize_error(line) for line in feedback_lines if line and line.strip())
        if key
    )


# # ── Deterministic (no-LLM) repair of mechanically-correctable lint errors ────

# _REQUIRE_PARENS = re.compile(r"\brequire\s*\(([^;\n]*?)\)\s*;")
# _REQUIRE_MSG_STR = re.compile(r'\brequire\s+([^,;\n]+?)\s*,\s*"[^"]*"\s*;')


# def _apply_deterministic_repairs(spec_content: str, lint_errors) -> tuple[str, bool]:
#     """Surgical fixes for mechanically-correctable lint classes — NO LLM call.

#     Only unambiguous, safe rewrites are attempted (require-paren removal, dropping
#     Solidity error-string messages). Semantic errors are left for the LLM.
#     Returns (patched_spec, changed).
#     """
#     categories = {err.category for err in lint_errors}
#     if not (categories & {"require_parens", "invalid_require"}):
#         return spec_content, False
#     # Strip parens first so `require(x, "msg")` becomes `require x, "msg";`,
#     # then drop the unsupported Solidity error-string argument.
#     patched = _REQUIRE_PARENS.sub(
#         lambda m: f"require {m.group(1).strip()};", spec_content,
#     )
#     patched = _REQUIRE_MSG_STR.sub(
#         lambda m: f"require {m.group(1).strip()};", patched,
#     )
#     return (patched, patched != spec_content)


# ── Preserving ghost/hook/definition declarations across the parallel merge ───

class SpecGenerator:
    """Generate CVL specifications for Solidity contracts."""

    def __init__(self, config=None):
        self.config = config or get_config()
        self.vector_db = VectorDBManager(self.config)
        self.last_validation: Optional[ValidationResult] = None
        self.last_status: str = "skipped"
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
        validate: bool = True,
        certora_contract_name: Optional[str] = None,
        validation_timeout: int = 300,
        project_root: Optional[str] = None,
        remappings_file: Optional[str] = None,
        certora_config: Optional[str] = None,
        max_repairs: int = 5,
        parallel: bool = True,
    ) -> str:

        project = load_project(contract_path, project_root, remappings_file)
        resolved_path = project.entrypoint
        contract_code = project.source_text
        contract_name = certora_contract_name or detect_contract_name(
            resolved_path.read_text(encoding="utf-8", errors="replace"), resolved_path.stem
        )
        if project.unresolved_imports:
            print("Warning: unresolved imports: " + ", ".join(project.unresolved_imports))

        # Contract fingerprint for retrieval + error memory
        query = query or contract_profile(contract_code, contract_name)
        contract_hash = ErrorMemory.hash_contract(contract_code) # CHECK
        run_id = ErrorMemory.new_run_id()

        print(f"Query: {query}")

        # Fetch known errors from prior runs
        known_errors = self.error_memory.all_known_errors(contract_hash) # CHECK
        if known_errors:
            print(f"📝 Loaded {len(known_errors)} known-bad patterns from prior runs")

        # Retrieve reference specs
        retrieved_context = self.vector_db.query(query, top_k=top_k) #CHECK

        if not retrieved_context:
            print("Warning: No similar specs found in database (or all below similarity floor)")
        else:
            print(f"Found {len(retrieved_context)} reference specs")

        # Build deterministic methods block (Phase 4)
        det_methods_block = build_methods_block(contract_code) #CHECK - Why are we building the raw method block it should be done by llm this could be dangerous or we could double check using llm and maybe do some pruning
        print(f"Built deterministic methods block ({det_methods_block.count(chr(10))} entries)")

        # Generate spec using LLM
        print(f"Generating CVL spec using {self.config.LLM_PROVIDER}/{self.config.LLM_MODEL}...")
        if parallel:
            spec_content = _parallel_generate(
                self,
                contract_code=contract_code,
                retrieved_context=retrieved_context,
                contract_name=contract_name,
                methods_block=det_methods_block,
                known_errors=known_errors,
            )
        else:
            spec_content = _call_llm(
                self,
                contract_code=contract_code,
                retrieved_context=retrieved_context,
                contract_name=contract_name,
                known_errors=known_errors,
            )

        spec_content = _clean_cvl_spec(spec_content, det_methods_block, contract_code)

        validation_output_path = self.save_spec(spec_content, output_path, 0)

        # A6: record the outcome so callers (CLI/CI) can act on it. Statuses:
        #   passed  — certora compilation succeeded
        #   failed  — validation attempted, could not be made to pass
        #   unverified — validation requested but certora unavailable / env error
        #   skipped — validation not requested
        final_status = "skipped" if not validate else "unverified"

        if validate:
            prev_spec_hash = None
            consecutive_counts: dict[str, int] = {}
            escalated_keys: set[str] = set()
            feedback_lines: list[str] = []
            for attempt in range(max_repairs):
                # Phase 2: run static linter before Certora
                # lint_errors = lint_spec(spec_content, contract_code)
                lint_errors = None
                if lint_errors:
                    print(f"🔍 Linter caught {len(lint_errors)} issues (skipping Certora)")
                    # Mechanical classes are fixed deterministically — never ask the
                    # LLM to remove require() parens or an error-string by hand.
                    # patched, mechanically_fixed = _apply_deterministic_repairs(spec_content, lint_errors)
                    # if mechanically_fixed:
                    #     print("🛠  Deterministic repair applied; re-linting before any LLM call...")
                    #     spec_content = _clean_cvl_spec(patched, det_methods_block, contract_code)
                    #     continue
                    feedback_lines = [e.message for e in lint_errors]
                    result = validate_cvl(
                            resolved_path, validation_output_path, contract_name,
                            validation_timeout, project.root, certora_config
                        )
                    error_source = "lint"
                else:
                    validation_output_path.write_text(spec_content)
                    result = validate_cvl(
                        resolved_path, validation_output_path, contract_name,
                        validation_timeout, project.root, certora_config
                    )
                    self.last_validation = result
                    if result.passed:
                        print(f"✅ Certora compilation passed on attempt {attempt + 1}")
                        self.error_memory.record(contract_hash, run_id, attempt, spec_content, [])
                        output_path_for_passed = output_path + "_passed" if output_path is not None else "Output/" + contract_name + ".spec"
                        self.save_spec(spec_content, output_path_for_passed, attempt + 1)
                        final_status = "passed"
                        break
                    if _is_environment_error(result.output):
                        print(f"⚠️  Environment error (not a spec issue): {result.output[:200]}")
                        print("   Fix your environment and re-run. Aborting repair loop.")
                        final_status = "unverified"
                        break
                    feedback_lines = _enrich_certora_feedback(result.output, spec_content)
                    error_source = "certora"


                # known_errors_before = self.error_memory.all_known_errors(contract_hash)
                # known_keys_before = frozenset(normalize_error(k) for k in known_errors_before if k)


                # self.error_memory.record(contract_hash, run_id, attempt, spec_content, feedback_lines)
                # known_errors = self.error_memory.all_known_errors(contract_hash)

                # error_keys = _normalized_error_keys(feedback_lines)
                # for key in list(consecutive_counts):
                #     if key not in error_keys:
                #         del consecutive_counts[key]
                # for key in error_keys:
                #     consecutive_counts[key] = consecutive_counts.get(key, 0) + 1
                # max_repeats = getattr(self.config, "MAX_CONSECUTIVE_SAME_ERRORS", 2)
                # repeat_key = next(
                #     (k for k, c in consecutive_counts.items() if c >= max_repeats),
                #     None,
                # )
                # if repeat_key is not None:
                #     if repeat_key in escalated_keys:
                #         self._write_failure_report(
                #             contract_name, spec_content, feedback_lines,
                #             repeat_key, output_path, attempt,
                #         )
                #         print(f"🛑 Giving up on recurring error after escalation. "
                #               f"Report saved next to the spec output.")
                #         final_status = "failed"
                    
                #     escalated_keys.add(repeat_key)
                #     print(f"⚠️  Error '{repeat_key}' is recurring. Escalating to HARD REPAIR mode...")
                #     spec_content = self._call_llm(
                #         contract_code=contract_code,
                #         retrieved_context=retrieved_context,
                #         contract_name=contract_name,
                #         repair_feedback=feedback_lines,
                #         previous_spec=spec_content,
                #         known_errors=known_errors,
                #         known_keys_before=known_keys_before,
                #         hard_repair=True,
                #     )
                #     spec_content = _clean_cvl_spec(spec_content, det_methods_block, contract_code)
                #     validation_output_path = self.save_spec(spec_content, output_path, attempt + 1)
                #     continue

                print(f"Static check failed (attempt {attempt+1}, source={error_source})")
                spec_content = _call_llm(
                    self,
                    contract_code=contract_code,
                    retrieved_context=retrieved_context,
                    contract_name=contract_name,
                    repair_feedback=feedback_lines,
                    previous_spec=spec_content,
                    known_errors=known_errors,
                    known_keys_before=None,
                )
                spec_content = _clean_cvl_spec(spec_content, det_methods_block, contract_code)

                # Short-circuit if LLM returned a byte-identical spec (nothing changed)
                current_hash = hashlib.sha256(spec_content.encode()).hexdigest()
                if current_hash == prev_spec_hash:
                    print("⚠️  LLM returned an identical spec — repair loop stuck. Aborting.")
                    final_status = "failed"
                    # break
                prev_spec_hash = current_hash

                validation_output_path = self.save_spec(spec_content, output_path, attempt + 1)

                print("-" * 50 + "START" + "-" * 50)
                print(f"Attempt {attempt+1} ({error_source}) failures:")

                for err in feedback_lines:
                    print(f"  • {err}")
                print("-" * 50 + "END" + "-" * 50)
            else:
                # Loop exhausted without passing — still record final state
                self.error_memory.record(contract_hash, run_id, max_repairs, spec_content, feedback_lines)
                final_status = "failed"
        else:
            # No validation — still record for future runs
            self.error_memory.record(contract_hash, run_id, 0, spec_content, list[str]())

        self.last_status = final_status
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

    # def _write_failure_report(
    #     self,
    #     contract_name: str,
    #     spec_content: str,
    #     feedback_lines: list[str],
    #     repeat_key: str,
    #     output_path: Optional[str],
    #     attempt: int,
    # ) -> Path:
    #     """Write a human-readable report when the repair loop gives up, so the
    #     job ends with actionable output instead of a silent abort."""
    #     if output_path:
    #         p = Path(output_path)
    #         report = p.parent / f"{p.stem}_REPAIR_FAILED.md"
    #     else:
    #         report = Path(self.config.OUTPUT_DIR) / f"{contract_name}_REPAIR_FAILED.md"
    #     report.parent.mkdir(parents=True, exist_ok=True)
    #     report.write_text(
    #         f"# Repair loop gave up — {contract_name}\n\n"
    #         f"The error below recurred for {attempt + 1} consecutive repair rounds and "
    #         "survived both the deterministic fixer and a hard-repair LLM pass. Continuing "
    #         "would only burn more LLM calls, so the loop stopped. The last spec is saved "
    #         "as the matching `*_attempt*.spec` file.\n\n"
    #         "## Unfixable recurring error\n\n"
    #         f"```\n{repeat_key}\n```\n\n"
    #         "## Errors at give-up\n\n"
    #         + "\n".join(f"- {e}" for e in feedback_lines)
    #         + "\n\n## Common manual fixes by class\n\n"
    #         "- `forbidden_sum` — declare the ghost it references (e.g. `ghost mapping(address => uint256) mirror_admins;`) and add the matching `hook`/`init_state` axiom.\n"
    #         "- `undeclared_ghost` / `undeclared_env` — add the missing declaration or `env e;`.\n"
    #         "- `require_parens` / `invalid_require` — write `require condition;` with no parentheses and no error string.\n"
    #         "- `envfree_with_env` — drop the env argument from envfree getters.\n"
    #         "- anything else — apply the error text directly to the last spec attempt.\n"
    #     )
    #     print(f"📝 Repair-failure report written to: {report}")
    #     return report


# ponytail: simple keyword match — covers known certoraRun/solc error messages.
# If Certora invents new env-error phrasing, add it here.
_ENV_ERROR_PATTERNS = [
    "solc not found",
    "solidity executable",
    "certorarun is not installed",
    "certorakey",
    "connection refused",
    "no --solc path given",
]


def _is_environment_error(output: str) -> bool:
    """True if the Certora failure is an env issue the LLM can't fix."""
    lowered = output.lower()
    return any(pat in lowered for pat in _ENV_ERROR_PATTERNS)




def _enrich_certora_feedback(output: str, spec_content: str) -> list[str]:
    """Extract Certora error messages and attach corresponding code line snippets."""
    spec_lines = spec_content.splitlines()
    enriched = []

    # Mapping of error substrings to helpful suggestions
    error_suggestions = {
        "unexpected token near `(": "Function calls must have a single argument list. Rewrite `f(x)(e, y)` as `f(x, e, y);`.",
        "require` in `require(": "Remove parentheses around condition. Write `require condition;` not `require(condition);`.",
        "Variable `e` has not been declared": "Add `env e;` as the first statement in the rule/invariant that uses `e`.",
        "sum()": "Do not use `sum()` on raw mappings. Use a ghost variable with a hook instead.",
        "before` / `after` keywords": "Do not use `before` / `after` keywords in CVL.",
        "old(`": "Do not use `old(...)` or `@old` in CVL.",
    }

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
            # Add suggestion if any known pattern matches
            for key, suggestion in error_suggestions.items():
                if key in err_msg:
                    enriched.append(f"  Suggestion: {suggestion}")
                    break
        elif "Syntax error:" in line_str or "TypeError:" in line_str or "Undefined" in line_str:
            enriched.append(line_str)
            # Provide suggestions for syntax errors
            if 'unexpected token near `("' in line_str:
                enriched.append("  Suggestion: Function calls must have a single argument list. Rewrite `f(x)(e, y)` as `f(x, e, y);`.")
        elif "Variable `" in line_str and "has not been declared" in line_str:
            enriched.append(line_str)
            enriched.append("  Suggestion: Add `env e;` as the first statement in the rule/invariant that uses `e`.")

    return enriched if enriched else [l for l in output.splitlines() if l.strip() and not l.startswith("WARNING") and not l.startswith("Compiling")]


def main() -> None:
    """CLI entrypoint for generating a CVL spec from a Solidity contract."""
    parser = argparse.ArgumentParser(description="Generate a CVL specification for a Solidity contract")
    parser.add_argument("contract", help="Path to the Solidity contract file")
    parser.add_argument("--query", "-q", default=None, help="Search query to use for retrieving reference specs")
    parser.add_argument("--top_k", type=int, default=None, help="Number of reference specs to retrieve")
    parser.add_argument("--output", "-o", default=None, help="Path to save the generated .spec file")
    parser.add_argument("--quiet", action="store_true", help="Do not print the generated spec to stdout")
    parser.add_argument("--no-check", action="store_true", help="Skip Certora compilation of the generated spec (exit 0; spec is unverified)")
    parser.add_argument("--contract-name", default=None, help="Contract name passed to Certora (defaults to the file stem)")
    parser.add_argument("--validation-timeout", type=int, default=300, help="Certora compilation timeout in seconds")
    parser.add_argument("--project-root", default=None, help="Solidity project root (defaults to the contract directory)")
    parser.add_argument("--remappings", default=None, help="Foundry remappings.txt file")
    parser.add_argument("--certora-config", default=None, help="Existing Certora .conf/.json input for validation")
    parser.add_argument("--no-parallel", action="store_true", help="Disable parallel per-function drafting")
    args = parser.parse_args()

    generator = SpecGenerator()
    spec = generator.generate(
        contract_path=args.contract,
        query=args.query,
        top_k=args.top_k,
        output_path=args.output,
        validate=not args.no_check,
        certora_contract_name=args.contract_name,
        validation_timeout=args.validation_timeout,
        project_root=args.project_root,
        remappings_file=args.remappings,
        certora_config=args.certora_config,
        parallel=not args.no_parallel,
    )

    if not args.quiet:
        print(spec)
    sys.exit(_exit_code_for_status(getattr(generator, "last_status", "skipped")))


if __name__ == "__main__":
    main()
