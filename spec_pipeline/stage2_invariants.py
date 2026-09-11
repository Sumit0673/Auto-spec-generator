"""
Stage 2: Invariant Mining

Walks the Stage 1 table and classifies each state variable into one of:
- immutable-once-set
- monotonic
- access-gated-write
- cross-contract-mirrored
- free

This is the stage that needs JUDGMENT (LLM reasoning), not extraction.
Separation from Stage 1: extraction hallucinates fields, mining hallucinates importance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .stage1_extract import Stage1Table, FirstPartyContract
from .llm_client import get_llm_client
from .prompts import (
    STAGE2_SYSTEM,
    format_stage2_user,
)
from spec_pipeline.utils import read_source as _read_source


# ---------------------------------------------------------------------------
# Initializer-aware immutable-once-set classification (R19.3)
# ---------------------------------------------------------------------------
#
# A proxy-pattern (upgradeable) contract initializes its state in an
# ``initialize()`` function guarded by an ``initializer`` / ``reinitializer``
# modifier instead of in a constructor (the constructor of a logic contract is
# never run behind a proxy). A state variable written ONLY in the constructor
# and/or such an initializer is still "immutable-once-set" - it is assigned once
# during setup and never changed afterward. Without treating the initializer as a
# writer, such a variable would be misclassified as ``free`` because no ordinary
# function writes it (Requirement 19.3).
#
# ``_is_initializer`` is a pure predicate over a function name and its access
# modifier, so its truth table is exercised offline without slither. The
# classification helper below consumes only the duck-typed Stage 1 dataclasses
# (``StateVarInfo.writers`` + ``FunctionGate``), so it is likewise testable with
# hand-built inputs.

# The setup-writer categories treated as "writes only during construction".
_CONSTRUCTOR_NAMES = frozenset({"constructor"})
_INITIALIZER_MODIFIERS = frozenset({"initializer", "reinitializer"})


def _is_initializer(function_name: str, modifier: str) -> bool:
    """Return True when a function acts as an initializer for R19.3 purposes.

    A function is treated as an initializer when EITHER:

    * it carries a modifier named ``initializer`` or ``reinitializer`` (the
      OpenZeppelin upgradeable guards), OR
    * its name is ``initialize`` or begins with ``initialize`` (e.g.
      ``initialize``, ``initializeV2``, ``initialize_pool``).

    The check is case-sensitive on the name prefix (Solidity identifiers are
    case-sensitive) but tolerates ``None``/empty inputs by treating them as
    absent. ``reinitializer(2)`` style modifiers are matched by base name so a
    parameterized reinitializer guard still counts.

    Args:
        function_name: The function's name (e.g. ``"initialize"``).
        modifier: The function's access-control modifier, or ``"none"``/empty.

    Returns:
        ``True`` if the function should be treated as a writer for the
        immutable-once-set classification, else ``False``.
    """
    name = (function_name or "").strip()
    mod = (modifier or "").strip()

    # Modifier match: strip any argument list, e.g. "reinitializer(2)".
    mod_base = mod.split("(", 1)[0].strip()
    if mod_base in _INITIALIZER_MODIFIERS:
        return True

    # Name match: exactly "initialize" or an "initialize"-prefixed name.
    if name == "initialize" or name.startswith("initialize"):
        return True

    return False


def _gate_modifier(contract: "FirstPartyContract", function_name: str) -> str:
    """Return the access modifier recorded for ``function_name`` on ``contract``.

    Looks up the matching ``FunctionGate`` (external/public functions and the
    constructor are recorded as gates). Returns ``"none"`` when the function has
    no gate entry, so a writer that is an internal helper simply carries no
    initializer modifier.
    """
    for gate in getattr(contract, "function_gates", []):
        if gate.name == function_name:
            return gate.modifier
    return "none"


def _is_setup_only_writer(
    contract: "FirstPartyContract", writer_name: str
) -> bool:
    """Return True when ``writer_name`` only writes state during setup (R19.3).

    A setup-only writer is the constructor or an initializer (per
    :func:`_is_initializer`, using the writer's recorded gate modifier). A state
    variable whose writers are all setup-only writers is immutable-once-set.
    """
    if writer_name in _CONSTRUCTOR_NAMES:
        return True
    modifier = _gate_modifier(contract, writer_name)
    return _is_initializer(writer_name, modifier)


def classify_immutable_once_set(
    contract: "FirstPartyContract", state_var: "StateVarInfo"
) -> bool:
    """Return True when ``state_var`` is immutable-once-set (R19.3).

    A variable qualifies when it is written at all AND every one of its writers
    is a setup-only writer - the constructor or an initializer / reinitializer
    guarded (or ``initialize``-named) function. This is the classification path
    that treats initializers as writers so an upgradeable contract's
    initialize-only state is not misclassified as ``free``.

    Constant / immutable variables are already immutable by the compiler and are
    not re-derived here (they are handled by their own category upstream); this
    predicate concerns mutable storage assigned once during setup.

    Args:
        contract: The owning :class:`FirstPartyContract` (for gate lookup).
        state_var: The :class:`StateVarInfo` under classification.

    Returns:
        ``True`` when the variable is written only by setup-only writers, else
        ``False`` (including when it has no writers at all).
    """
    writers = list(getattr(state_var, "writers", []))
    if not writers:
        # Never written by any tracked function: not "written once", so this
        # predicate does not claim it (a never-written var is handled elsewhere).
        return False
    return all(_is_setup_only_writer(contract, w) for w in writers)


def mine_invariants(
    table: Stage1Table,
    source_path: str | Path,
    output_dir: str | Path | None = None,
    llm_client=None,
) -> dict:
    """
    Stage 2: Mine invariants from Stage 1 table using LLM judgment.

    Args:
        table: Stage1Table from Stage 1
        source_path: Path to original .sol file(s) for context
        output_dir: Output directory
        llm_client: LLM client (optional, will create if None)

    Returns:
        dict: {contract_name: {var_name: {category, direction, modifier, mirror, rationale, risk}}}
    """
    if llm_client is None:
        llm_client = get_llm_client()

    # Read source code for context
    source_code = _read_source(source_path)

    # Format Stage 1 table as text
    stage1_text = table.to_text()

    # Call LLM
    print("Stage 2: Mining invariants...")
    user_prompt = format_stage2_user(stage1_text, source_code)
    response = llm_client.call(STAGE2_SYSTEM, user_prompt, temperature=0.1)

    # Parse JSON from response
    invariants = _parse_invariants(response)

    # Export
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = Path(source_path).stem if Path(source_path).is_file() else "project"
        out_file = output_dir / f"{base_name}_stage2_invariants.json"
        with open(out_file, "w") as f:
            json.dump(invariants, f, indent=2, default=str)
        print(f"Exported Stage 2 invariants to {out_file}")

    return invariants





def _parse_invariants(response: str) -> dict:
    """Parse LLM JSON response into invariants dict, handling truncation."""
    # Try to extract JSON from markdown block
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON
    try:
        start = response.find("{")
        end = response.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(response[start:end])
    except json.JSONDecodeError:
        pass

    # Try to fix truncated JSON by finding the last complete object
    try:
        # Find the last complete contract entry
        start = response.find("{")
        if start != -1:
            # Look for the last complete "}" before truncation
            json_str = response[start:]
            # Try to balance braces
            open_braces = 0
            last_valid_end = -1
            for i, ch in enumerate(json_str):
                if ch == '{':
                    open_braces += 1
                elif ch == '}':
                    open_braces -= 1
                    if open_braces == 0:
                        last_valid_end = i + 1
            if last_valid_end > 0:
                candidate = json_str[:last_valid_end]
                return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fallback: empty dict
    print("Warning: Could not parse Stage 2 invariants JSON, returning empty")
    return {}