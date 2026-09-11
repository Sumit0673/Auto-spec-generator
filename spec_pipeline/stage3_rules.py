"""
Stage 3: Per-Contract Rule Writing

Input: Stage 1 table + Stage 2 invariants (as FIXED INPUT, not to rediscover)
Output: CVL spec with parametric rules for modifier cohorts

Key instruction: "If N functions share modifier M and the same invariant pattern,
write ONE parametric rule, not N rules."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .stage1_extract import Stage1Table, FirstPartyContract
from .llm_client import get_llm_client
from .prompts import (
    STAGE3_SYSTEM,
    format_stage3_user,
)
from .utils import read_source as _read_source
from .methods_block import generate_methods_block
from .cvl import extract_cvl


def write_rules(
    table: Stage1Table,
    invariants: dict,
    source_path: str | Path,
    output_dir: str | Path | None = None,
    llm_client=None,
) -> str:
    """
    Stage 3: Write CVL rules using Stage 2 invariants as given input.

    Args:
        table: Stage1Table from Stage 1
        invariants: Dict from Stage 2 {contract: {var: {category, ...}}}
        source_path: Path to original .sol file(s)
        output_dir: Output directory
        llm_client: LLM client

    Returns:
        str: Full CVL spec
    """
    if llm_client is None:
        llm_client = get_llm_client()

    source_code = _read_source(source_path)
    stage1_text = table.to_text()
    methods_block = generate_methods_block(table, source_code).text

    print("Stage 3: Writing CVL rules...")
    user_prompt = format_stage3_user(stage1_text, invariants, source_code, methods_block)
    response = llm_client.call(STAGE3_SYSTEM, user_prompt, temperature=0.2)

    # Extract CVL from response
    cvl_spec = extract_cvl(response, methods_block)

    # Export
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = Path(source_path).stem if Path(source_path).is_file() else "project"
        out_file = output_dir / f"{base_name}_stage3_spec.cvl"
        with open(out_file, "w") as f:
            f.write(cvl_spec)
        print(f"Exported Stage 3 spec to {out_file}")

    return cvl_spec
