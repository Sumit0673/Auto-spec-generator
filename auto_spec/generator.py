"""
Main specification generator using RAG + LLM.
"""

import argparse
import os
import re
import sys
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
from auto_spec.retrieval import contract_profile
from auto_spec.solidity_project import detect_contract_name, load_project
from auto_spec.vector_db import VectorDBManager
from auto_spec.prompts import format_property_gpt_prompt, extract_cvl_spec



class SpecGenerator:
    """Generate CVL specifications for Solidity contracts."""
    
    def __init__(self, config=None):
        self.config = config or get_config()
        self.vector_db = VectorDBManager(self.config)
        self.last_validation: Optional[ValidationResult] = None
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
        max_repairs: int = 10
    ) -> str:
        """
        Generate CVL specification for a Solidity contract.
        
        Args:
            contract_path: Path to Solidity contract file
            query: Search query (defaults to contract source)
            top_k: Number of reference specs to retrieve
            output_path: Path to save generated spec file
            
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
        
        # Match the same compact Solidity representation used by the vector index.
        query = query or contract_profile(contract_code, contract_name)

        print(f"Query: {query}")
        
        # Retrieve reference specs
        print(f"Retrieving similar specs for: {contract_name}...")
        retrieved_context = self.vector_db.query(query, top_k=top_k)

        if not retrieved_context:
            print("Warning: No similar specs found in database")
        else:
            print(f"Found {len(retrieved_context)} reference specs")
            for i, ctx in enumerate(retrieved_context):
                # adjust field names to whatever your retrieval object actually returns
                print(f"  [{i}] {getattr(ctx, 'name', ctx)!r} score={getattr(ctx, 'score', '?')}")
        
        # Generate spec using LLM
        print(f"Generating CVL spec using {self.config.LLM_OPENROUTER_MODEL}...")
        spec_content = self._call_llm(contract_code=contract_code, retrieved_context=retrieved_context, contract_name=contract_name)
        spec_content = _strip_solidity_mutability_keywords(spec_content)

        validation_output_path = self.save_spec(spec_content, output_path, 0)

        if validate:
            for attempt in range(max_repairs):
                result = validate_cvl(contract_path, validation_output_path, contract_name, validation_timeout, project.root, certora_config)
                if result.passed:
                    break
                print(f"Static check failed (attempt {attempt+1})")
                spec_content = self._call_llm(
                    contract_code=contract_code,
                    retrieved_context=retrieved_context,
                    contract_name=contract_name,
                    repair_feedback=result.output.splitlines(),
                    previous_spec=spec_content,
                )
                spec_content = _strip_solidity_mutability_keywords(spec_content)
                validation_output_path = self.save_spec(spec_content, output_path, attempt+1)

                print("#" * 50)
                print(f"Certora compilation of attempt {attempt+1}: {result.output}")
                print("#" * 50)


        return spec_content    

    def save_spec(self, spec_content: str, output_path: Optional[str], attempt: int) -> Path:
        """Save the generated CVL spec to a file."""
        if not output_path:
            output_path = self.config.OUTPUT_DIR / "generated_spec.cvl"
        else:
            output_path = Path(output_path + str(attempt))
            if not output_path.suffix:
                output_path = output_path.with_suffix(".cvl")
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(spec_content)
        print(f"✓ CVL spec saved to: {output_path}")
        return output_path
    
    def _call_llm(
        self,
        contract_code: str,
        retrieved_context: list,
        contract_name: str,
        repair_feedback: Optional[list[str]] = None,
        previous_spec: Optional[str] = None,

    ) -> str:
        system_prompt, user_prompt = format_property_gpt_prompt(
            contract_code=contract_code,
            retrieved_context=retrieved_context,
            contract_name=contract_name
        )
        system_prompt = f"{system_prompt}"
        temperature = self.config.LLM_TEMPERATURE
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
            temperature = 0  #

        try:
            provider = self.config.LLM_PROVIDER

            OPENAI_COMPAT_BASE_URLS = {
                "openai": None,
                "nvidia": self.config.LLM_BASE_URL,
                "deepseek": "https://api.deepseek.com",
                "openrouter": "https://openrouter.ai/api/v1",
            }

            if provider in OPENAI_COMPAT_BASE_URLS:
                extra_headers = {}
                if provider == "openrouter":
                    # Optional but recommended by OpenRouter for routing/rate-limit visibility.
                    extra_headers = {
                        "HTTP-Referer": getattr(self.config, "APP_URL", "https://localhost"),
                        "X-Title": getattr(self.config, "APP_NAME", "auto-spec"),
                    }

                client = OpenAI(
                    base_url=OPENAI_COMPAT_BASE_URLS[provider],
                    api_key=self.config.LLM_API_KEY,
                )
                response = client.chat.completions.create(
                    model=self.config.LLM_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=2000,
                    temperature=temperature,
                )
                raw_response = response.choices[0].message.content.strip()

            elif provider == "gemini":
                client = genai.Client()
                response = client.models.generate_content(
                    model=self.config.LLM_GEMINI_MODEL,
                    contents=user_prompt,
                    config={
                        "system_instruction": system_prompt,
                        "temperature": self.config.LLM_TEMPERATURE,
                    },
                )
                raw_response = response.text.strip()

            else:
                raise ValueError(f"Unknown LLM provider: {provider}")

            spec_content = extract_cvl_spec(raw_response)
            if not spec_content:
                raise RuntimeError(
                    "LLM response did not contain recognizable CVL; no spec was saved. "
                    "Adjust the prompt or retry with different retrieved context."
                )
            return spec_content

        except Exception as e:
            raise RuntimeError(f"Error calling LLM: {e}")


def _strip_solidity_mutability_keywords(spec_content: str) -> str:
    """Best-effort auto-repair: strip Solidity mutability keywords from methods
    block entries instead of relying on the LLM to remove them correctly."""
    return re.sub(r'\b(payable|view|pure|nonpayable)\b\s*', '', spec_content)


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
    )

    if not args.quiet:
        print(spec)


if __name__ == "__main__":
    main()
