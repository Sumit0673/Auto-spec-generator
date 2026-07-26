# Contributing to Auto-Spec

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing to the Auto-Spec project.

## Code of Conduct

Please be respectful and constructive in all interactions.

## How to Contribute

### 1. Reporting Issues

Found a bug? Have a feature request?

- Check if the issue already exists
- Provide a clear description
- Include steps to reproduce (for bugs)
- Share your environment (Python version, OS, etc.)

### 2. Submitting Pull Requests

**Before starting:**
1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Run tests: `pytest tests/ -v`
5. Format code: `black auto_spec/ tests/`
6. Lint: `flake8 auto_spec/ tests/`

**When submitting:**
1. Push to your fork
2. Create a Pull Request with clear description
3. Link related issues
4. Ensure CI passes

### 3. Development Setup

```bash
git clone https://github.com/yourusername/auto-spec.git
cd auto-spec
pip install -e ".[dev]"
```

### 4. Testing

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=auto_spec --cov-report=html

# Run specific test
pytest tests/test_generator.py::test_function -v
```

### 5. Code Style

We use:
- **black** for code formatting
- **isort** for import sorting
- **flake8** for linting
- **mypy** for type checking

```bash
black auto_spec/ tests/ examples/
isort auto_spec/ tests/ examples/
flake8 auto_spec/ tests/
mypy auto_spec/
```

## Areas for Contribution

### High Priority

- [ ] Additional LLM provider support (Claude, Gemini, etc.)
- [ ] Improved error handling and validation
- [ ] Comprehensive test suite
- [ ] Better documentation

### Medium Priority

- [ ] Support for custom embedding models
- [ ] Spec validation and quality metrics
- [ ] Integration with CI/CD systems
- [ ] Performance optimizations

### Nice to Have

- [ ] Web UI for spec generation
- [ ] Visualization tools
- [ ] Spec comparison utilities
- [ ] Community spec database

## Commit Message Guidelines

Use clear, descriptive commit messages:

```
type(scope): brief description

Longer description explaining the changes made.

Fixes #123
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

## Documentation

- Update README.md for user-facing changes
- Add docstrings to all functions
- Include type hints
- Update examples if relevant

## Questions?

Open an issue or discussion for questions!

Thank you for contributing! 🎉
