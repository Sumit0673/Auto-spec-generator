# spec_pipeline

`spec_pipeline` takes one Solidity file or one Solidity project and produces a
CVL (Certora Verification Language) specification that has actually been run
through `certoraRun`, together with an honest report of what the Certora Prover
concluded. A spec is never presented as verified unless a prover verdict exists
for every rule it contains. When verification cannot be completed, the run
terminates with exactly one classified outcome that names the blocking cause.

## The five stages

The pipeline is a five-stage sequence orchestrated by `spec_pipeline/pipeline.py`.
Each stage writes a typed artifact (`<base>_stageN.json`) that later stages read.

1. **Stage 1 — Extractor.** Runs slither to produce the first-party contract
   table: contracts, state variables, function gates, and caller edges.
2. **Stage 2 — Invariant Miner.** Classifies each state variable into one
   invariant category (LLM-driven).
3. **Stage 3 — Rule Writer.** Produces CVL rules, invariants, hooks, and ghost
   declarations. Emits the deterministic `methods` block from the Stage 1 table
   and extracts CVL non-destructively. An iterative variant (Repair Loop)
   re-prompts using prover diagnostics.
4. **Stage 4 — Critic.** Produces adversarial sibling-function findings and
   folds them into the spec.
5. **Stage 5 — Verifier.** Invokes `certoraRun` with the remappings and solc
   the project needs, and produces the Verification Report with a single
   Verification Status.

The user-facing deliverable (the Spec Bundle) is the final `.spec` file, the
Verification Report, and the Run Manifest.

## Install and setup

Supported Python: **>=3.10, <3.15** (declared in `pyproject.toml`).

Run the bootstrap script from a clean virtual environment of a supported Python
version with network access. It installs every pinned Python dependency and
prints the resolved version of each external tool.

```bash
python -m venv .venv
source .venv/bin/activate
scripts/bootstrap.sh
```

Dependencies (name and exact pins) are declared in `pyproject.toml`:

- Runtime: `slither-analyzer`, `networkx`, `openai`, `pdfplumber`
- Test: `pytest`, `pytest-cov`, `hypothesis`

To install directly with pip instead of the bootstrap script:

```bash
pip install -e .          # runtime dependencies
pip install -e '.[test]'  # runtime + test dependencies
```

## Commands

Core pipeline entry point:

```bash
python -m spec_pipeline <path> \
  [--stages 1 2 3 4 5] \
  [--stage N] \
  [--output-dir DIR] \
  [--certora-args "..."] \
  [--iterative-stage3] \
  [--max-iterations N]
```

- `<path>` — a Solidity file or a project directory to analyze.
- `--stages` — the set of stages to run (default: all five).
- `--stage N` — run a single stage.
- `--output-dir DIR` — where artifacts and the Spec Bundle are written.
- `--certora-args "..."` — extra arguments passed through to `certoraRun`.
- `--iterative-stage3` — use the Repair Loop variant of Stage 3.
- `--max-iterations N` — Repair Loop iteration cap (default 3, range 1-10).

Newer flags (existing or planned) that add capability while preserving prior
default behavior:

- `--no-cache` — run every requested stage from its inputs; do not read
  artifacts already on disk.
- `--require-cache` — exit with code 3 and name the missing or stale artifact
  when a requested stage's prerequisite is absent or stale.
- `--allow-missing-tools` — run the stages whose tools resolved and record the
  outcome `skipped_missing_tool` for each remaining stage.
- `--deps-root PATH` — additional shared Solidity dependency root(s) to search
  (repeatable), in addition to `SOLIDITY_DEPS_ROOT` and discovered
  `node_modules`/`lib`.
- `--verify-contract NAME` — when the Stage 1 table declares more than one
  first-party contract, verify the named contract(s).
- `--autofix-cvl` — apply only the semantics-preserving rewrites in the CVL
  autofix allowlist and record each applied rewrite.

### Evaluation harness

Scores the pipeline against the human-written pairs in `Paired_Dataset` and
records a baseline used by the quality gate.

```bash
python -m spec_pipeline.evaluation [--jobs N] [--resume] [--update-baseline]
```

- `--jobs N` — run up to N pipeline invocations concurrently (default 1).
- `--resume` — skip pairs that already have a complete record for the current
  source fingerprint.
- `--update-baseline` — overwrite the evaluation baseline only when every floor
  and tolerance check passes.

### Hygiene check

Reports version-control hygiene violations (tracked paths that match ignore
rules, tracked compiled Python artifacts). Exits 0 when clean, 1 otherwise.

```bash
python -m spec_pipeline.hygiene_check
```

## External tools

The pipeline invokes these external tools. Each is required only by the stages
listed. Minimum versions are the lowest known-good versions for this pipeline;
`scripts/bootstrap.sh` prints the versions actually resolved on your machine.

| Tool | Required by | Minimum version | Install command |
|---|---|---|---|
| `solc` (via slither) | Stage 1 (Extractor), Stage 5 (Verifier) | 0.5.0 | `pip install solc-select` then `solc-select install <ver> && solc-select use <ver>` |
| `certoraRun` | Stage 5 (Verifier) | 7.0.0 | `pip install certora-cli` |
| `node` + `npm` | Stage 5 for Hardhat/npm import resolution | node 18, npm 9 | `nvm install 18` (or install from nodejs.org) |
| GitHub CLI (`gh`) | Dataset scraping / repository tooling (off the run path) | 2.0.0 | `brew install gh` / `sudo apt install gh` |

slither itself is installed as the pinned `slither-analyzer` Python dependency
by `scripts/bootstrap.sh`.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `LLM_BASE_URL` | provider default (no explicit default) | Base URL of the LLM endpoint used by stages 2-4. |
| `LLM_MODEL` | no default | Model name requested from the LLM provider; recorded in the Run Manifest. |
| `LLM_API_KEY` | no default | API key for the LLM provider. Its value is redacted (written as `REDACTED`) in every artifact and log. |
| `LLM_MAX_TOKENS` | provider default (no explicit default) | Maximum completion tokens requested per LLM call. |
| `LLM_CACHE_DIR` | unset (caching disabled) | Directory for the LLM record/replay cache. When set, responses are keyed on the SHA-256 of the system prompt, user prompt, model, and temperature, enabling byte-identical replays. |
| `SOLIDITY_DEPS_ROOT` | unset | Additional shared Solidity dependency root(s) searched by the Dependency Resolver, alongside `--deps-root` and discovered `node_modules`/`lib`. |
| `CERTORA_DATASET_ROOT` | repository root inferred from module location | Base directory the audit-report scraper and evaluation harness treat as the `Paired_Dataset` root. |

Any environment variable whose name ends with `_API_KEY`, `_TOKEN`, or
`_SECRET` has its value redacted in all artifacts and logs.

## Outcomes and exit codes

Every run terminates with exactly one member of the Outcome Set. The CLI exits
with code 0 only when the status is `verified` or `verified_with_warnings`, and
with a distinct documented nonzero code for every other outcome. The table
below is the single source of truth (kept consistent with the design's Data
Models section), with one operator action per outcome.

| Outcome | Exit | Meaning | Operator action to resolve |
|---|---|---|---|
| `verified` | 0 | every rule passing, non-vacuous, no warnings | None. The spec is verified; ship the Spec Bundle. |
| `verified_with_warnings` | 0 | all passing, no vacuity, ≥1 prover warning | Review the prover warnings in the Verification Report; address them if they affect intent. |
| `violated` | 1 | ≥1 rule failing | Inspect the failing rule's counterexample in the Verification Report; fix the contract or correct the rule. |
| `vacuous` | 2 | all passing but ≥1 vacuous rule | Strengthen the preconditions of each named vacuous rule so its premises are reachable. |
| `no_first_party_contracts` | 4 | zero first-party contracts to analyze | Point `<path>` at the project's own sources; check the dependency roots did not filter out your contracts. |
| `tool_unavailable` | 5 | a required tool is absent (no `--allow-missing-tools`) | Install the named tool (see External tools), or rerun with `--allow-missing-tools` to skip the blocked stages. |
| `typecheck_failed` | 6 | CVL typechecker rejected the spec | Read the recorded typechecker diagnostics; rerun with `--iterative-stage3` (and optionally `--autofix-cvl`) to repair the CVL. |
| `compile_failed` | 7 | slither/solc could not compile sources | Read the compiler diagnostics; supply the correct `--deps-root`/remappings and an installed solc that matches the pragmas. |
| `no_compatible_solc` | 8 | no installed solc satisfies pragmas | Install a solc version satisfying the reported constraints, e.g. `solc-select install <ver>`. |
| `unsupported_pragma_set` | 9 | first-party files declare disjoint pragmas | Reconcile the conflicting pragma constraints in the named files, or analyze them separately. |
| `llm_unavailable` | 10 | LLM endpoint rejected the probe | Verify `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`; confirm network access and provider availability. |
| `skipped_missing_tool` | 0 | stage skipped under `--allow-missing-tools` | Install the named tool and rerun without `--allow-missing-tools` to execute the skipped stage. |
| `timeout` | 11 | prover/run budget elapsed | Narrow the scope (`--verify-contract`), simplify rules, or raise the budget via `--certora-args`. |
| `error` | 12 | unhandled/other | Read the captured traceback recorded for the run and file the failure; rerun after addressing the cause. |
