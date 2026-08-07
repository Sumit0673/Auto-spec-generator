"""Prompts module initialization."""

from auto_spec.prompts.property_gpt import (
    format_property_gpt_prompt,
    format_per_function_prompt,
    format_cross_cutting_prompt,
    PROPERTY_GPT_SYSTEM_PROMPT,
    analyze_solidity_contract,
    extract_cvl_spec,
)

__all__ = [
    "format_property_gpt_prompt",
    "format_per_function_prompt",
    "format_cross_cutting_prompt",
    "PROPERTY_GPT_SYSTEM_PROMPT",
    "analyze_solidity_contract",
    "extract_cvl_spec",
]
