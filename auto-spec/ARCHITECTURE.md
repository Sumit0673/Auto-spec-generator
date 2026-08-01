# Architecture and Design

## Overview

Auto-Spec is a production-ready tool for automated CVL specification generation. It uses Retrieval-Augmented Generation (RAG) with embedding-based similarity search and Large Language Models to generate formal specifications.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User Interface                           │
│                  CLI / Python API / Web UI                      │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                      Generator (Core)                           │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ SpecGenerator: Orchestrates the generation pipeline     │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
        ▼                      ▼                      ▼
   ┌─────────┐          ┌────────────┐         ┌──────────┐
   │ Config  │          │ Vector DB  │         │   LLM    │
   │         │          │            │         │          │
   │ - Keys  │          │ - Chroma   │         │ - OpenAI │
   │ - Models│          │ - Embedder │         │ - NVIDIA │
   │ - Paths │          │ - Query    │         │ - Claude │
   └─────────┘          └────────────┘         └──────────┘
        │                      │                      │
        └──────────────────────┼──────────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │ Prompt Generator     │
                    │ (PropertyGPT Format) │
                    └──────────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │ Output Parser       │
                    │ Extract Sections    │
                    └──────────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │ CVL Specification   │
                    └─────────────────────┘
```

## Component Architecture

### 1. Config (`auto_spec/config.py`)
- Centralized configuration management
- Environment variable loading
- Validation of required settings
- Support for multiple LLM providers and models

### 2. Vector DB Manager (`auto_spec/vector_db.py`)
- Chroma vector store initialization
- Model loading (sentence-transformers)
- Query and retrieval operations
- Database download from remote sources
- Checksum verification

### 3. Prompt Module (`auto_spec/prompts/`)
- PropertyGPT system prompt
- Prompt template formatting
- In-context example formatting
- Section parsing

### 4. Generator (`auto_spec/generator.py`)
- Main orchestration logic
- Contract reading
- Vector DB querying
- LLM API calls
- Spec generation and saving

### 5. CLI (`auto_spec/cli.py`)
- Command-line interface
- Argument parsing
- User-friendly output
- Command routing

## Data Flow

```
1. User Input
   │
   ├── Contract Path
   ├── Query (optional)
   └── Output Path (optional)
   │
2. Contract Analysis
   │
   ├── Read Solidity code
   └── Extract metadata
   │
3. Vector Database Lookup
   │
   ├── Embed query (sentence-transformers)
   ├── Search Chroma DB
   └── Retrieve top-k similar specs
   │
4. Prompt Construction
   │
   ├── System prompt (expert instructions)
   ├── Reference specs (in-context examples)
   ├── Target contract
   └── Query context
   │
5. LLM Invocation
   │
   ├── Call API (NVIDIA NIM / OpenAI)
   ├── Receive response
   └── Parse output
   │
6. Output Formatting
   │
   ├── Extract SECTION 1 (overview)
   ├── Extract SECTION 2 (spec)
   ├── Save to file
   └── Return to user
```

## Vector Database

### Pre-built Database Structure

```
chroma_db/
├── chroma.sqlite3          # Main database file
├── manifests/              # Metadata
├── index/                  # Vector index
└── collection_data.json    # Collection info
```

### Collection Schema

Each document in the collection contains:
```json
{
  "id": "unique-id",
  "document": "CVL specification content",
  "metadatas": {
    "contract_name": "ContractName",
    "spec_filename": "spec.spec",
    "contract_type": "ERC20",
    "properties": ["transfer", "approval"],
    "source": "AaveV3"
  },
  "embedding": [vector...]
}
```

### Remote Database Distribution

To avoid shipping the entire vector database:

1. Upload to cloud storage (S3, GCS, etc.)
2. Create manifest file with checksums
3. Point users to remote URL via environment variable
4. Download on first use with integrity verification

## Configuration Management

### Priority Order

1. Environment variables (highest priority)
2. .env file in project root
3. Configuration object passed to generator
4. Default values (lowest priority)

### Validation

```python
config = Config()
is_valid, error_msg = config.validate()
```

Checks:
- LLM API key is set
- Vector database exists or remote URL is configured
- Output directory is writable

## LLM Provider Integration

### Supported Providers

```python
# NVIDIA NIM
OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=NVIDIA_API_KEY
)

# OpenAI
OpenAI(api_key=OPENAI_API_KEY)

# Custom endpoints
OpenAI(
    base_url="https://your-endpoint/v1",
    api_key=YOUR_API_KEY
)
```

### Temperature and Parameters

- `temperature=0.2` for deterministic, technical output
- Tunable via `config.LLM_TEMPERATURE`
- Other parameters can be extended in `generator.py`

## Prompt Engineering (PropertyGPT)

### Approach

1. **System Prompt**: Expert instructions for CVL generation
2. **In-Context Examples**: Similar specs retrieved from DB
3. **Target Contract**: User's Solidity code
4. **Query**: Search context from user

### Two-Section Output

- **SECTION 1**: Natural language property candidates
- **SECTION 2**: Formal CVL code

## Error Handling

### Graceful Degradation

- Missing API key → Clear error message
- Vector DB not found → Suggest setup command
- LLM error → Retry logic + error reporting
- Empty retrieval results → Continue with generation

### Validation

- Contract file must exist
- API credentials must be valid
- LLM response must be parseable
- Output directory must be writable

## Performance Considerations

### Optimization Areas

- **Embedding Caching**: Cache embeddings of frequently used queries
- **Batch Processing**: Support batch specification generation
- **Parallel Retrieval**: Query multiple LLMs in parallel
- **Database Indexing**: Optimize Chroma DB index

### Scalability

- Vector DB size: ~500MB for current specs
- Query latency: ~1-2 seconds (depends on LLM)
- Memory footprint: ~2GB with all dependencies

## Future Enhancements

1. **Web UI**: Browser-based interface
2. **Spec Validation**: Automatic syntax checking
3. **Quality Metrics**: Measure spec quality
4. **Custom Models**: Support for fine-tuned models
5. **Spec Comparison**: Diff and merge utilities
6. **Community DB**: User-contributed specs
