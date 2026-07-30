# Python Code Style

- Follow Ruff configuration from `pyproject.toml`.
- Use type hints for public interfaces and non-obvious values.
- Keep shared-core and `plugin.py` syntax compatible with Python 3.9.
- Use immutable dataclasses for neutral value objects where appropriate.
- Validate external mappings before constructing domain objects.
- Keep Home Assistant imports outside the neutral core.
- Prefer focused pytest tests and descriptive test names.
- Run Ruff format and lint checks before pushing.
