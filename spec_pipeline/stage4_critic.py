"""
Stage 4: Adversarial Critic

Input: Raw source + Stage 3 draft spec + Stage 1 table
Output: JSON findings array

Looks for:
1. Unchecked zero-values on sensitive params
2. Missing validity checks that exist in siblings
3. Inconsistent access modifiers
4. Inconsistent validation patterns among siblings
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .stage1_extract import Stage1Table
from .llm_client import get_llm_client
from .prompts import (
    STAGE4_SYSTEM, format_stage4_user,
    STAGE4_REPAIR_SYSTEM, format_stage4_repair_user,
)
from .cvl import extract_cvl
from spec_pipeline.utils import read_source as _read_source


def criticize(
    source_path: str | Path,
    draft_spec: str,
    table: Stage1Table,
    output_dir: str | Path | None = None,
    llm_client=None,
) -> list[dict]:
    """
    Stage 4: Adversarial critique of draft spec.

    Args:
        source_path: Original .sol file(s)
        draft_spec: CVL spec from Stage 3
        table: Stage1Table from Stage 1
        output_dir: Output directory
        llm_client: LLM client

    Returns:
        List of findings dicts
    """
    if llm_client is None:
        llm_client = get_llm_client()

    source_code = _read_source(source_path)
    stage1_text = table.to_text()

    print("Stage 4: Adversarial critique...")
    user_prompt = format_stage4_user(source_code, draft_spec, stage1_text)
    response = llm_client.call(STAGE4_SYSTEM, user_prompt, temperature=0.1)

    # Parse findings
    findings = _parse_findings(response)

    # Export
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = Path(source_path).stem if Path(source_path).is_file() else "project"
        out_file = output_dir / f"{base_name}_stage4_critique.json"
        with open(out_file, "w") as f:
            json.dump({"findings": findings}, f, indent=2, default=str)
        print(f"Exported Stage 4 critique to {out_file}")

    return findings





def _parse_findings(response: str) -> list[dict]:
    """Parse LLM JSON response into findings list."""
    # Try markdown block
    json_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try raw array
    try:
        start = response.find("[")
        end = response.rfind("]") + 1
        if start != -1 and end > start:
            return json.loads(response[start:end])
    except json.JSONDecodeError:
        pass

    print("Warning: Could not parse Stage 4 findings JSON, returning empty")
    return []


def apply_findings(
    findings: list[dict],
    draft_spec: str,
    source_path: str | Path,
    output_dir: str | Path | None = None,
    llm_client=None,
) -> str:
    """Apply Stage 4 findings to the draft spec via LLM repair call.

    Returns:
        str: Corrected CVL spec with findings applied
    """
    if not findings:
        print("No findings to apply, spec unchanged")
        return draft_spec

    if llm_client is None:
        llm_client = get_llm_client()

    source_code = _read_source(source_path)

    high = sum(1 for f in findings if f.get("severity") == "high")
    print(f"Stage 4 repair: applying {len(findings)} findings ({high} high-severity)...")

    user_prompt = format_stage4_repair_user(draft_spec, findings, source_code)
    response = llm_client.call(STAGE4_REPAIR_SYSTEM, user_prompt, temperature=0.1)

    repaired = extract_cvl(response)

    # Export repaired spec
    if output_dir:
        output_dir = Path(output_dir)
        base_name = Path(source_path).stem if Path(source_path).is_file() else "project"
        out_file = output_dir / f"{base_name}_stage4_repaired.cvl"
        out_file.write_text(repaired)
        print(f"Exported repaired spec to {out_file}")

    return repaired