from openai import OpenAI
from google import genai
from openai import OpenAI
from typing import Optional
from auto_spec.prompts import extract_cvl_spec, format_property_gpt_prompt
from auto_spec.error_memory import normalize_error
from auto_spec.config import get_config


def _call_llm(
        self,
        contract_code: str,
        retrieved_context: list,
        contract_name: str,
        repair_feedback: Optional[list[str]] = None,
        previous_spec: Optional[str] = None,
        known_errors: list[str] | None = None,
        known_keys_before: frozenset[str] | None = None,
        hard_repair: bool = False,
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
            # Flag problems that have already been reported in a prior run/iteration:
            # "RECURRING" tells the LLM the previous fix did not take effect and this
            # exact error must be prioritized, not re-approached as if new.
            recurring_keys = known_keys_before or frozenset()
            annotated_issues = []
            for issue in repair_feedback:
                key = normalize_error(issue)
                marker = (
                    "  [RECURRING — already reported before; the previous fix did not "
                    "take effect. This exact error MUST be fixed now, do not re-approach it as new.]"
                    if key and key in recurring_keys
                    else ""
                )
                annotated_issues.append(f"- {issue}{marker}")
            feedback_block = "\n".join(annotated_issues)
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
                if hard_repair:
                    user_prompt += (
                        "\n\n### HARD REPAIR MODE\n"
                        "PREVIOUS repair attempts failed to fix the SAME errors listed above. "
                        "The exact problems have recurred unchanged, which means a prior edit "
                        "either missed them or re-introduced them. Make the SMALLEST possible "
                        "change that resolves ONLY the listed problems — if the error says a "
                        "ghost or declaration is missing, ADD that declaration; if it says a "
                        "construct is forbidden, REMOVE or REPLACE that construct. Do not "
                        "reformulate the surrounding rule. If a problem genuinely cannot be "
                        "fixed, keep the rest of the spec valid and say so after SECTION 1."
                    )
            else:
                user_prompt += (
                    "\n\nThe previous spec you generated failed with these problems:\n"
                    f"{feedback_block}\n\nRegenerate the FULL spec, fixing every issue listed."
                )
            temperature = 0

        raw_response = _raw_llm_call(self, system_prompt, user_prompt, temperature)
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
            return (response.text or "").strip()

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
        print("======================================================")
        print(f"Response: {response.choices[0].message.content}")
        print("======================================================")
        return (response.choices[0].message.content or "").strip()

    except Exception as e:
        raise RuntimeError(f"Error calling LLM: {e}")
