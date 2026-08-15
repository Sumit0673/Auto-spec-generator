# Auto-Spec

Auto-Spec is a Python tool for generating Certora CVL specifications from Solidity smart contracts using retrieval-augmented generation (RAG) and LLMs.

## Features
- Generate CVL specifications from Solidity source
- Use a local or remote Chroma vector database
- Retrieve similar verified specs as context for generation
- Works with NVIDIA and OpenAI-compatible models

## Quick start

### 1. Install
```bash
cd auto-spec
pip install -e .
```

### 2. Set up the vector database
Use the local database shipped with this repo:
```bash
export CHROMA_DB_PATH=/home/sumit-gupta/Auto-Spec/erc20_pairs_final/chroma_db
```

Or use a remote release URL:
```bash
export CHROMA_DB_REMOTE_URL=https://github.com/Sumit0673/Auto-spec-generator/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz
```

### 3. Add your API key
```bash
export NVIDIA_API_KEY=your-key-here
```

### 4. Generate a spec
```bash
/home/sumit-gupta/Auto-Spec/.venv/bin/auto-spec generate /home/sumit-gupta/Auto-Spec/erc20_pairs_final/mock_token.sol --query "ERC20 transfer and allowance rules" -o /tmp/mock_token.spec
```

## Repository layout
- auto-spec/: Python package and CLI
- erc20_pairs_final/: dataset, specs, and vector database assets


**Architectural Roadmap**
--------------------------------------------------------------

This document outlines the end-to-end technical architecture for an automated, multi-agent AI system designed to generate Certora Verification Language (CVL) .spec files from Solidity smart contracts. By combining static analysis, contextual retrieval-augmented generation (RAG), and dynamic invariant mining, the system bypasses the typical limitations of standalone LLMs to produce production-grade formal verification blueprints.

**Step 1: The Code Parsing Agent (Static Analysis / AST)**
----------------------------------------------------------

**Context**
-----------

LLMs inherently struggle with precise token counting, variable tracking, and mapping deeply nested execution trees across large codebases. This agent mitigates those shortcomings by replacing raw text inference with hard-coded syntactic validation.

**How It Works**
----------------

The agent executes a hard-coded parser framework (such as solc or solidity-parser) directly against the target smart contract. This parser processes the raw Solidity codebase and translates it into a deterministic Abstract Syntax Tree (AST).

**Agent's Output**
------------------

The agent compiles a clean, strictly structured layout containing:

*   **State Variables:** A definitive list of every state variable along with its associated visibility modifier (public, internal, private).
    
*   **Function Dependency Map:** A complete call graph detailing how control flows through the contract (e.g., _Function A_ calls _Function B_, which triggers _Modifier C_).
    
*   **Execution Safety Bounds:** Identification of all explicit revert(), require(), and assert() conditions embedded within the codebase.
    

**Why It's Critical**
---------------------

This structural blueprint serves as the mathematical foundation for the spec file. It maps out the exact boilerplate code required for the CVL .spec file's methods block, completely eliminating the risk of LLM hallucinations regarding variable names, visibility boundaries, or function signatures.

**Step 2: The Intent Agent (Contextual RAG)**
---------------------------------------------

**Context**
-----------

Formal verification requires an explicit understanding of system intent; a mathematical model cannot verify correctness if it does not know what the program is _supposed_ to do. If an asset bridge or lending pool contains a structural flaw that allows uncollateralized withdrawals, a standard LLM looking only at the code might mistakenly assume that behavior is intentional.

**How It Works**
----------------

Prior to examining any executable code, this agent ingests the project's natural language documentation. It processes files such as whitepapers, GitBook documentation, official design requirements, and repository README files. This unstructured text is partitioned, vectorized, and stored in a specialized vector database.

**Agent's Output**
------------------

The agent dynamically queries the vector database using semantic search strings to extract core business rules, including:

*   **Economic Parameters:** Assertions like _"Users must maintain a minimum 150% collateral ratio at all times."_
    
*   **Access Control Limits:** Assertions like _"The contract administrator is strictly barred from directly withdrawing user-deposited capital."_
    

**Why It's Critical**
---------------------

This layer extracts human-readable documentation strings and refines them into high-level semantic properties. These security properties provide the guardrails necessary for subsequent agents to translate human business logic into strict mathematical theorems.

**Step 3: The Testing Agent (Dynamic Invariant Mining)**
--------------------------------------------------------

**Context**
-----------

Relying entirely on text analysis limits an AI's ability to discover emergent system behaviors. To understand deep mathematical invariants, the architecture requires an observation layer that evaluates how the smart contract operates under pressure.

**How It Works**
----------------

The agent deploys the Solidity smart contract into an isolated, local EVM simulation environment (such as Foundry or Hardhat). It then executes automated property-based fuzz testing, firing thousands of randomized transactions at the contract's exposed interfaces.

**Agent's Output**
------------------

The agent continuously monitors the contract's state variables before and after every state transition to uncover truths that remain constant across all execution paths. It outputs discovered invariants, such as:

*   **Constant Relationships:** _"Across all execution branches and swap paths, the product of tokenABalance \* tokenBBalance either increases or remains exactly equal."_
    

**Why It's Critical**
---------------------

Dynamic mining catches complex, low-level mathematical axioms (such as automated market maker constant-product formulas or algorithmic fee scaling) that are remarkably difficult for a traditional language model to deduce through code reading alone.

**Step 4: The Synthesizer Agent (Generating the .spec)**
--------------------------------------------------------

**Context**
-----------

The final layer of the architecture serves as the compiler and quality assurance engine, converting the multi-modal agent insights into a single, cohesive formal verification document.

**How It Works**
----------------

A master synthesis agent aggregates the definitive telemetry collected by the three previous structural layers. It feeds these contextual tokens into a highly tailored code-generation model optimized for formal logic systems.

**Synthesis Input Matrix**
--------------------------

The Synthesizer processes three explicit inputs:

1.  **The Syntactic Framework:** The exact variable and method schema generated by the Static Analysis agent.
    
2.  **The Behavioral Intent Rules:** The natural-language semantic restrictions discovered by the Contextual RAG agent.
    
3.  **The Discovered Mathematical Axioms:** The runtime data properties established by the Dynamic Invariant Mining agent.
    

**Final Output**
----------------

The agent reconciles these inputs to author and output a fully formed, compilable .spec file written natively in **Certora Verification Language (CVL)**, complete with fully structured methods blocks, state invariants, and explicit transaction rules.