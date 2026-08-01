# QUICKSTART.md

Get Auto-Spec up and running in 5 minutes!

## 1. Install

```bash
pip install auto-spec
```

Or from source:
```bash
git clone https://github.com/yourusername/auto-spec.git
cd auto-spec
pip install -e .
```

## 2. Setup API Key

```bash
# For NVIDIA NIM (recommended)
export NVIDIA_API_KEY="your-api-key-here"

# Or for OpenAI
export OPENAI_API_KEY="your-api-key-here"
export LLM_PROVIDER=openai
export LLM_MODEL=gpt-4
```

## 3. Download Vector Database

```bash
auto-spec setup
```

This downloads ~500MB of pre-built CVL specifications from similar smart contracts.

## 4. Generate Your First Spec

```bash
# Simple usage
auto-spec generate MyToken.sol

# With output file
auto-spec generate MyToken.sol -o MyToken.spec

# With custom query
auto-spec generate MyToken.sol --query "ERC20 transfer and approval rules"
```

## 5. Check Configuration

```bash
auto-spec config
```

## Python API

```python
from auto_spec import SpecGenerator

generator = SpecGenerator()
spec = generator.generate("path/to/contract.sol")
print(spec)
```

## Troubleshooting

**"LLM API Key not found"**
- Set NVIDIA_API_KEY or OPENAI_API_KEY environment variable

**"Chroma DB not found"**
- Run `auto-spec setup` to download the vector database

**"No similar specs found"**
- This is OK! The tool will still generate a specification
- Try with a more specific --query

## Next Steps

- See [README.md](README.md) for full documentation
- Check [examples/](examples/) for advanced usage
- Review generated specs with formal verification experts

## Support

- GitHub Issues: [Report bugs](https://github.com/yourusername/auto-spec/issues)
- Discussions: [Ask questions](https://github.com/yourusername/auto-spec/discussions)
