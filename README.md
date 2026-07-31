# Auto-Spec

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/downloads/)

**Automated Certora Verification Language (CVL) Specification Generation for Smart Contracts**

Auto-Spec uses Retrieval-Augmented Generation (RAG) and Large Language Models (LLMs) to automatically generate formal CVL specifications from Solidity smart contracts. It leverages in-context learning with similar verified specs to produce high-quality formal specifications.

## Features

✨ **Automated Spec Generation** - Generate complete CVL specifications from Solidity code
🔍 **RAG-Powered** - Retrieves similar verified specs for in-context learning
🤖 **LLM Integration** - Supports NVIDIA NIM, OpenAI, and other LLM providers
📦 **Easy Installation** - One-line pip install with pre-built vector database
🔧 **Configurable** - Customize models, parameters, and output
💾 **Production-Ready** - Proper error handling, logging, and documentation
📚 **Extensible** - Plugin support for custom prompts and validators

## Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/auto-spec.git
cd auto-spec

# Install with pip
pip install -e .

# Or install from PyPI
pip install auto-spec
```

### 2. Setup Vector Database

The tool includes a pre-built vector database of verified CVL specifications. Download it on first use:

```bash
auto-spec setup
```

Alternatively, configure a remote URL in your `.env`:
```bash
CHROMA_DB_REMOTE_URL=https://github.com/Sumit0673/Auto-spec-generator/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz
```

### 3. Configure LLM Access

Set your LLM API key:

```bash
# For NVIDIA NIM (default)
export NVIDIA_API_KEY=your-api-key

# Or for OpenAI
export OPENAI_API_KEY=your-api-key
export LLM_PROVIDER=openai
export LLM_MODEL=gpt-4
```

### 4. Generate a Specification

```bash
# Basic usage
auto-spec generate path/to/MyToken.sol

# With custom query
auto-spec generate path/to/MyToken.sol --query "ERC20 transfer and approval rules"

# Save to file
auto-spec generate path/to/MyToken.sol -o output/MyToken.spec
```

## Usage

### Command Line Interface

```bash
# Generate specification
auto-spec generate <contract_path> [OPTIONS]

Options:
  --query, -q TEXT        Search query for reference specs
  --top_k INTEGER         Number of reference specs (default: 3)
  --output, -o PATH       Output path for .spec file
  --quiet                 Don't print spec to stdout

# View configuration
auto-spec config

# Setup/update database
auto-spec setup

# Show version
auto-spec --version
```

### Python API

```python
from auto_spec import SpecGenerator

# Create generator
generator = SpecGenerator()

# Generate specification
spec = generator.generate(
    contract_path="path/to/MyToken.sol",
    query="ERC20 transfer rules",
    top_k=3,
    output_path="output/MyToken.spec"
)

print(spec)
```

#### Advanced Configuration

```python
from auto_spec import Config, SpecGenerator

# Create custom configuration
config = Config()
config.LLM_MODEL = "gpt-4"
config.LLM_PROVIDER = "openai"
config.TOP_K_RESULTS = 5
config.OUTPUT_DIR = "specs"

# Use custom config
generator = SpecGenerator(config=config)
spec = generator.generate("path/to/contract.sol")
```

## Configuration

### Environment Variables

```bash
# LLM Configuration
LLM_PROVIDER=nvidia              # or: openai, anthropic
LLM_MODEL=meta/llama-3.1-70b-instruct
NVIDIA_API_KEY=...              # or OPENAI_API_KEY
LLM_TEMPERATURE=0.2

# Vector Database
CHROMA_DB_PATH=/path/to/chroma_db
CHROMA_DB_REMOTE_URL=https://...
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5

# RAG Configuration
TOP_K_RESULTS=3
SIMILARITY_THRESHOLD=0.3

# Output
OUTPUT_DIR=./auto_spec_output
```

### Configuration File (.env)

Create a `.env` file in your project root:

```env
NVIDIA_API_KEY=your-api-key
LLM_MODEL=meta/llama-3.1-70b-instruct
CHROMA_DB_REMOTE_URL=https://cdn.example.com/chroma_db/
TOP_K_RESULTS=3
```

## Vector Database Management

### Pre-built Database

The tool uses a pre-built vector database of 20+ verified CVL specifications:
- Aave Protocol (v3 tokens, delegates)
- Gho Token (stable debt, variable debt)
- Staked Aave (v1.5, v3)
- ERC20/ERC4626 examples
- And more...

**Database Size:** ~500MB (downloads automatically on first setup)

### Training Data Distribution

The vector database (`chroma_db/`) is kept separate from the code package:

✅ **Users download automatically:**
```bash
auto-spec setup  # Downloads ~500MB once
```

✅ **Multiple source options:**
- **GitHub Releases** (recommended, free) - Default
- **AWS S3** (faster, optional)
- **Custom URLs** (your own storage)
- **Local copy** (development)

**See [TRAINING_DATA_STRATEGY.md](TRAINING_DATA_STRATEGY.md) for complete distribution setup.**

### For Maintainers: Distributing Your Database

**GitHub Releases (Automated):**
```bash
git tag -a v1.0.0 -m "Release"
git push origin v1.0.0
# GitHub Actions packages and releases chroma_db automatically
```

**AWS S3 (Manual or Automated):**
```bash
python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db
# Creates manifest.json and uploads all files
```

**See [DEPLOYMENT.md](DEPLOYMENT.md) for full deployment guide.**

### Adding New Specs

To extend the vector database with custom specifications:

```python
from auto_spec.vector_db import VectorDBManager
from pathlib import Path

db = VectorDBManager()
# Implement custom spec ingestion...
```

## Output Format

Auto-Spec generates CVL specifications in the PropertyGPT format with two main sections:

### SECTION 1: Property Overview

Natural language descriptions of candidate properties organized by category:
- **State Invariants**: Global properties that must always hold
- **Transfer & Arithmetic Rules**: Pre/post-condition rules
- **Access Control Rules**: Authorization and permission rules
- **Edge Cases**: Zero-address, overflow, re-entrancy checks

### SECTION 2: Formal CVL Specification

Production-ready CVL code including:
- Method declarations
- Invariant definitions
- Rule implementations
- Ghost variables and hooks (if needed)

## Examples

See the `examples/` directory for:
- [Basic usage](examples/basic_usage.py) - Simple spec generation
- [Advanced usage](examples/advanced_usage.py) - Custom configuration and parsing

## Development

### Installation (Development)

```bash
git clone https://github.com/yourusername/auto-spec.git
cd auto-spec
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest tests/ -v
pytest tests/ --cov=auto_spec  # With coverage
```

### Code Quality

```bash
# Format code
black auto_spec/ tests/ examples/

# Lint
flake8 auto_spec/ tests/

# Type checking
mypy auto_spec/
```

## Architecture

```
auto-spec/
├── auto_spec/
│   ├── config.py          # Configuration management
│   ├── vector_db.py       # Vector store operations
│   ├── generator.py       # Main spec generator
│   ├── cli.py            # CLI interface
│   ├── prompts/          # LLM prompt templates
│   └── utils/            # Helper utilities
├── scripts/              # Database management scripts
├── examples/             # Usage examples
├── tests/                # Test suite
└── docs/                 # Documentation
```

## How It Works

1. **Contract Analysis**: Reads the Solidity contract
2. **Spec Retrieval**: Searches vector database for similar verified specs
3. **Prompt Construction**: Builds a PropertyGPT-style prompt with:
   - System prompt (expert instructions)
   - Reference specs (in-context examples)
   - Target contract code
   - Generated query
4. **LLM Generation**: Sends prompt to LLM for generation
5. **Output Formatting**: Extracts and formats the generated specification

## Supported LLM Providers

- **NVIDIA NIM** (default) - `meta/llama-3.1-70b-instruct`
- **OpenAI** - `gpt-3.5-turbo`, `gpt-4`, `gpt-4-turbo`
- **Anthropic Claude** - Coming soon
- **Open source models** - Via Ollama or custom endpoints

## Limitations

- Generated specs are **candidates** that require expert review
- LLM output quality varies based on contract complexity
- Vector database must be pre-built (currently no dynamic indexing)
- Requires valid LLM API credentials

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Areas for contribution:
- Additional embedding models
- Support for more LLM providers
- Improved prompt engineering
- Spec validation and quality metrics
- Documentation improvements

## Citation

If you use Auto-Spec in your research or project, please cite:

```bibtex
@software{autospec2024,
  title={Auto-Spec: Automated CVL Specification Generation},
  author={Your Team},
  year={2024},
  url={https://github.com/yourusername/auto-spec}
}
```

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Disclaimer

This tool is provided as-is for research and educational purposes. Generated specifications should **always be reviewed by formal verification experts** before use in production systems. The authors are not liable for any issues arising from use of this tool.

## Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/auto-spec/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/auto-spec/discussions)
- **Email**: support@example.com

## Acknowledgments

- Built on [Certora Verification Language](https://www.certora.com/)
- Powered by [Chroma](https://www.trychroma.com/) vector database
- Prompt design inspired by [PropertyGPT](https://arxiv.org/abs/2405.04762)
- Embeddings via [Sentence Transformers](https://www.sbert.net/)

---

**Made with ❤️ by the Auto-Spec team**
