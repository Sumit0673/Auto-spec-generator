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
