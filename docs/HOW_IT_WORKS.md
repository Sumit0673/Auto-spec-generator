# How It Works: A Plain-Language Guide to the Spec Generation Pipeline

## What this project does, in one sentence

It reads a smart contract (Solidity code) and automatically writes a **formal specification** — a set of machine-checkable rules describing what the contract should always do — then tries to **prove** those rules hold using a verification tool.

Think of it like a spell-checker for smart contracts, except instead of checking spelling, it checks that the money-handling logic can never misbehave, and it writes the checks for you.

## The cast of characters (external tools)

The pipeline leans on a few outside tools. On this machine they are all absent, so the whole system is built to run and be tested **offline** using recordings and stand-ins.

- **slither** — reads Solidity and tells us the contracts, functions, and variables inside (the "X-ray machine").
- **solc** — the Solidity compiler.
- **certoraRun** (the "prover") — takes the rules we wrote and mathematically checks them against the contract.
- **an LLM** (large language model) — the "writer" that drafts the rules in CVL (the prover's rule language).

## The five stages (the assembly line)

The contract flows through five stages, like a factory line. Each stage saves its work to a file (an "artifact") so later stages can pick up where earlier ones left off.

1. **Stage 1 — Extract** (`stage1_extract.py`): X-ray the contract. Produce a tidy table of every first-party contract, its state variables, its functions, who can call what, and whether it is a proxy. First-party means "code the author actually wrote," as opposed to imported library code.
2. **Stage 2 — Invariants** (`stage2_invariants.py`): Decide what *should* always be true of each variable — e.g. "this value is set once and never changes" or "this balance only ever goes up."
3. **Stage 3 — Write rules** (`stage3_rules.py`, `stage3_iterative.py`): The LLM drafts the actual CVL rules. If the first draft fails, a **repair loop** tries again a few times and keeps the *best* attempt (not just the last one).
4. **Stage 4 — Critic** (`stage4_critic.py`): A review pass that looks for problems in the drafted rules and suggests fixes.
5. **Stage 5 — Verify** (`stage5_verify.py`): Hand the rules to the prover and record the verdict — did every rule pass, honestly?

## The honesty rule (why this exists)

A naive system could cheat by reporting "verified!" when it actually proved nothing. This project is strict: it only reports **verified** when there is at least one rule, *every* rule passes, *nothing* was vacuous (a rule that passes only because it was never really tested), and there were zero warnings. Anything less gets a truthful label like `vacuous`, `violated`, or `typecheck_failed`.

## What can happen at the end (outcomes and exit codes)

Every run ends in exactly one outcome, and each outcome maps to a number the operating system sees (an "exit code"). Zero means success.

| Outcome | Exit code | Plain meaning |
|---|---|---|
| verified | 0 | Every rule passed, honestly. |
| verified_with_warnings | 0 | Passed, but the prover warned about something. |
| violated | 1 | At least one rule failed — the contract can misbehave. |
| vacuous | 2 | Rules "passed" but were empty of real meaning. |
| no_first_party_contracts | 4 | Nothing of the author's own to check. |
| tool_unavailable | 5 | A needed tool (like the prover) wasn't installed. |
| typecheck_failed | 6 | The rules didn't even parse. |
| compile_failed | 7 | The contract itself wouldn't compile. |
| no_compatible_solc | 8 | No matching Solidity compiler version. |
| unsupported_pragma_set | 9 | The files demand conflicting compiler versions. |
| llm_unavailable | 10 | The rule-writing model couldn't be reached. |
| skipped_missing_tool | 0 | A stage was skipped on purpose (tool missing, allowed). |
| timeout | 11 | The run took too long and was stopped. |
| error | 12 | An unexpected crash (captured, not hidden). |

## The supporting cast (the other files)

- `pipeline.py` — the **orchestrator**. Runs the five stages in order, decides when a saved artifact is still fresh vs. stale, and short-circuits early when there's nothing to do.
- `artifacts.py` — the **filing system**. Writes each stage's output in a strict, identical-every-time format with a "provenance" stamp (what produced it, from what source, when), so runs are reproducible.
- `cli.py` / `__main__.py` — the **command-line front door**; turns flags into behavior and returns the right exit code.
- `preflight.py` — the **pre-flight check**; before doing real work, confirms which tools are present and their versions.
- `resolve.py` — the **map reader**; figures out where the project's dependencies live and which compiler version to use (handles Foundry, Hardhat, and plain layouts).
- `prompts.py` / `prompt_lint.py` — the **instructions to the LLM**, plus a linter that blocks project-specific leftover names from leaking into those instructions (keeps the system general, not hard-wired to one codebase).
- `context.py` — the **budgeter**; when a contract is too big to send to the LLM whole, it picks the most relevant parts and records what it left out.
- `methods_block.py` / `cvl.py` — helpers that build the rule-file boilerplate and safely extract the LLM's CVL without destructively rewriting its meaning.
- `llm_client.py` / `llm_cache.py` — talk to the model, and **cache** every prompt/response so tests can replay them offline with no network.
- `outcomes.py` — the single source of truth for the outcome→exit-code table above.
- `eval/` — the **report card**: pairs generated specs against known-good ("ground truth") ones, computes quality metrics, runs the whole dataset in batch, and enforces a quality gate that fails the build if quality drops.

## How we know it works (the test strategy)

There are ~588 automated tests, all running offline. Two kinds:

- **Example tests** — "given this exact input, expect this exact output."
- **Property tests** — much stronger. Instead of one example, they generate 100+ random inputs and assert a rule that must hold for *all* of them. Categories include:
  - **round-trip**: save then load gives back the same thing.
  - **idempotence**: doing it twice equals doing it once.
  - **invariant**: a truth that always holds (e.g. the honesty rule for "verified").
  - **metamorphic**: changing the input in a known way changes the output only in the expected way (e.g. renaming a variable everywhere shouldn't change anything but the name; moving the project to a new folder shouldn't change the analysis).
  - **confluence**: running all five stages at once equals running them one at a time.
  - **order-independence**: shuffling the input order doesn't change the computed metrics.

Tool-facing behavior (things needing slither/solc/certoraRun/LLM) is covered by small tests using **recordings**, so nothing real is ever called during testing.

## The bottom line

Feed in a contract; get out an honest, machine-checkable specification and a truthful verdict about whether it holds — reproducibly, and without needing the network or the heavy tools installed just to develop and test the system.
