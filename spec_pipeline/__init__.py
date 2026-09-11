"""
spec_pipeline - 5-Stage Robust Spec Generation Pipeline

Stage 1: First-Party Extraction (slither-based, drops OZ/libraries)
Stage 2: Invariant Mining (LLM judgment)
Stage 3: Per-Contract Rule Writing (parametric rules for modifier cohorts)
Stage 4: Adversarial Critic (sibling-pattern bug detection)
Stage 5: Prover + Vacuity Check (certoraRun + rule coverage)

Each stage has distinct failure modes and is separately auditable.
"""

from pathlib import Path

# The five pipeline stages depend on slither (via solidity_graph). Importing
# them eagerly makes the whole package unusable on a machine without slither,
# which breaks import-safe subpackages such as ``spec_pipeline.eval`` (pure
# filesystem + regex, design R11). Guard the eager imports so the package still
# imports when slither is absent; the stage symbols are simply unavailable then,
# and any code path that actually needs a stage will raise a clear ImportError
# at call time via the normal ``spec_pipeline.stageN_*`` import.
try:
    from spec_pipeline.stage1_extract import FirstPartyExtractor, extract_first_party
    from spec_pipeline.stage2_invariants import mine_invariants
    from spec_pipeline.stage3_rules import write_rules
    from spec_pipeline.stage4_critic import criticize, apply_findings
    from spec_pipeline.stage5_verify import verify_with_prover
    from spec_pipeline.pipeline import run_pipeline
except ImportError:  # pragma: no cover - exercised only without slither installed
    FirstPartyExtractor = None
    extract_first_party = None
    mine_invariants = None
    write_rules = None
    criticize = None
    apply_findings = None
    verify_with_prover = None
    run_pipeline = None


# def read_source(source_path: str | Path) -> str:
#     """Read Solidity source code from a file or directory. Shared by all stages."""
#     source_path = Path(source_path)
#     if source_path.is_file():
#         return source_path.read_text(errors="ignore")

#     parts = []
#     for sol_file in sorted(source_path.rglob("*.sol")):
#         try:
#             parts.append(sol_file.read_text(errors="ignore"))
#         except OSError:
#             continue
#     return "\n\n".join(parts)

__all__ = [
    "FirstPartyExtractor",
    "extract_first_party",
    "mine_invariants",
    "write_rules",
    "criticize",
    "verify_with_prover",
    "run_pipeline",
]