<div align="center">

# 🔐 Auto-Spec

### AI-Powered Formal Verification for Solidity Smart Contracts

**Generate Certora CVL specifications automatically — from Solidity source code.**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Solidity](https://img.shields.io/badge/Solidity-0.8+-363636?style=for-the-badge&logo=solidity&logoColor=white)](https://soliditylang.org)
[![Certora](https://img.shields.io/badge/Certora-CVL-FF6B35?style=for-the-badge&logo=ethereum&logoColor=white)](https://www.certora.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](https://opensource.org/licenses/MIT)

</div>


## 🏗️ Runtime Pipeline (What Actually Executes)

The live pipeline implements this architecture with practical engineering tradeoffs:

| Stage | Implementation | Key Detail |
|-------|---------------|------------|
| **1. Profile & Retrieve** | `SolidityProject` + ChromaDB | Fingerprint contract → query vector DB for top-k similar verified specs |
| **2. Deterministic Methods Block** | `methods_block.py` | Pure AST parsing → no LLM.|
| **3. Parallel Rule Drafting** | `generator.py:_parallel_generate` | One LLM call per `external`/`public` function + one cross-cutting call |
| **4. Merge & Dedupe** | `_merge_specs` | Merges fragments by rule/invariant name; preserves ghost/hook declarations |
| **5. Auto-Repair** | `lint.py` + `_clean_cvl_spec` | Regex fixes + LLM semantic repair |
| **6. Certora Validation** | `certoraRun` | Loop if fails certora validation; error memory prevents repeat failures; exit codes reflect status |

---

## 🎯 The Problem

Writing formal verification specs for smart contracts is **painfully slow and error-prone**.

A single Certora CVL `.spec` file can take a security engineer **hours to days** of careful manual work — reading the contract, understanding the business logic, mapping function signatures, and writing mathematically precise rules. One wrong variable name or missed function, and the verifier rejects it.

> **What if an AI agent could do this in seconds?**

---

## 💡 The Solution

**Auto-Spec** is a multi-stage AI pipeline that reads your Solidity contract and produces a **compilable Certora CVL specification** — complete with method blocks, invariants, and per-function rules.

It doesn't just "ask ChatGPT to write a spec." It combines:

- 🔍 **RAG** — retrieves similar verified specs from a vector database as reference
- 🧠 **LLM** — drafts rules informed by real, proven verification patterns
- 🔧 **Deterministic repair** — a linter + auto-fix loop catches what the LLM misses
- ✅ **Validation** — the output compiles against Certora, with automatic retry on failure

---

## 🏗️ Architecture

```
                     ┌─────────────────┐
Solidity Contract ──►│  1. Profile &   │
                     │  Retrieve (RAG) │  
                     └────────┬────────┘
                              │
                     ┌────────▼────────┐
                     │ 2. Deterministic│
                     │  Methods Block  │  ← No LLM — pure parsing
                     └────────┬────────┘
                              │
                     ┌────────▼────────┐
                     │  3. Parallel    │
                     │  Rule Drafting  │  ← One LLM call per function
                     └────────┬────────┘
                              │
                     ┌────────▼────────┐
                     │  4. CVL Linter  │
                     │  + Auto-Repair  │  ← Deterministic fixes + LLM repair
                     └────────┬────────┘
                              │
                     ┌────────▼────────┐
                     │  5. Certora     │
                     │  Validation     │  ← Compilable .spec or retry
                     └────────┬────────┘
                              │
                     ┌────────▼────────┐
                     │  Output: .spec  │  ← Ready to verify ✓
                     └─────────────────┘
```

---

## ✨ Key Features

| Feature | What it does |
|---|---|
| 📄 **Smart Method Blocks** | Parsed from Solidity AST — not hallucinated.|
| 🔎 **RAG-Powered Context** | Retrieves top-k similar verified specs to ground the LLM in proven patterns. |
| ⚡ **Parallel Drafting** | One LLM call per external/public function — fast and isolated. |
| 🛠️ **CVL Linter** | Catches keyword leaks, missing getters, invalid requires, env errors, and more. |
| 🔄 **Auto-Repair Loop** | Deterministic regex fixes + LLM-powered semantic repair with stuck-loop detection. |
| 🧠 **Error Memory** | SQLite-backed pattern store — learns from past failures per contract hash. |
| 🔀 **Multi-Provider** | Works with OpenRouter, NVIDIA, OpenAI-compatible APIs — swap with one env var. |
| ✅ **Validation-First** | Output is validated against Certora by default. No silent passes on failure. |

---

## 🚀 Quick Start

```bash
# 1. Install
git clone https://github.com/Sumit0673/Auto-Spec.git
cd Auto-Spec
pip install -e .

# 2. Configure
export CHROMA_DB_PATH=./erc20_pairs_final/chroma_db
export OPENROUTER_API_KEY=your-key-here

# 3. Generate a spec
auto-spec generate contracts/MyToken.sol \
  --query "ERC20 transfer and allowance rules" \
  -o output/MyToken.spec

# 4. Validate (optional — runs by default)
auto-spec generate contracts/MyToken.sol \
  --query "ERC20 rules" -o output/MyToken.spec --check
```

---


## 🧪 Running Tests

```bash
pytest tests/ -v
```

**75 tests passing** — covering generation, linting, error memory, repair logic, and exit codes.

---

## 🗺️ Roadmap

- [ ] **Multi-contract support** — handle imports and cross-contract verification
- [ ] **Broader dataset** — expand beyond ERC20 to lending, AMMs, bridges, and governance
- [ ] **Web UI** — upload a contract, get a spec in your browser
- [ ] **Quality scoring** — automated metrics for spec completeness and Certora pass rate
- [ ] **Community spec DB** — crowdsourced verified specs for popular protocols

---

## 🔮 Future Architecture: The Full Multi-Agent Vision

The current runtime pipeline implements the **first two agents** (Static Analysis + RAG-based Intent) with LLM-assisted synthesis. The full vision expands to four specialized agents:

### **Agent 1: Code Parser (Static Analysis / AST)**
> *LLMs can't count tokens or track variables across nested call graphs reliably.*

- Runs `solc` / `solidity-parser` on the target contract
- Produces a **deterministic Abstract Syntax Tree**
- Outputs: state variables with visibility, complete function call graph, all `revert`/`require`/`assert` conditions
- **Why it matters:** Eliminates LLM hallucinations on variable names, function signatures, and visibility boundaries. The `methods {}` block is built from this — zero LLM involvement.
- **Status:** 🔮 **Planned**

### **Agent 2: Intent Extractor (Contextual RAG)**
> *Formal verification needs intent. Code alone doesn't tell you what the contract **should** do.*

- Ingests whitepapers, GitBook docs, design requirements, READMEs
- Vectorizes and stores in a specialized vector database
- Semantic search extracts **business rules as high-level properties**:
  - Economic invariants: *"Users must maintain 150% collateral ratio at all times"*
  - Access control: *"Admin cannot directly withdraw user deposits"*
- **Why it matters:** These semantic guardrails translate human business logic into mathematical theorems for the next stage.
- **Status:** 🔮 **Planned**

### **Agent 3: Invariant Miner (Dynamic Testing)**
> *Some invariants only emerge under execution pressure.*

- Deploys contract to a local EVM (Foundry/Hardhat)
- Runs property-based fuzzing — thousands of randomized transactions
- Monitors state before/after every transition to find **emergent mathematical axioms**:
  - *Constant-product formulas in AMMs*
  - *Algorithmic fee scaling relationships*
  - *Invariant balance relationships across swap paths*
- **Why it matters:** Catches low-level mathematical properties that are nearly impossible to deduce from code reading alone.
- **Status:** 🔮 **Planned**

### **Agent 4: Synthesizer (Spec Generation)**
> *The compiler and QA engine.*

- Aggregates all three prior outputs into a structured input matrix:
  1. **Syntactic Framework** (from Agent 1)
  2. **Behavioral Intent Rules** (from Agent 2)
  3. **Discovered Mathematical Axioms** (from Agent 3)
- Feeds into a code-generation model optimized for formal logic
- Outputs a **fully compilable CVL `.spec`** with methods blocks, state invariants, and transaction rules

> **Why this matters:** Each agent solves a subproblem that pure LLM text generation cannot — static analysis eliminates hallucinations, RAG grounds intent in documentation, dynamic mining finds invariants only visible under execution, and the synthesizer compiles it all into verified formal logic.

---

## 🤝 Contributing

Contributions are welcome! Whether it's new linter checks, more dataset pairs, or support for additional contract types — open an issue or PR.

```bash
# Development setup
pip install -e ".[dev]"
pytest tests/ -v
```

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">

**Built with ❤️ to make formal verification accessible to every Solidity developer.**

*If this project saves you time, consider giving it a ⭐ — it helps others find it.*

</div>
