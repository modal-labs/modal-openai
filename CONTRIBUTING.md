# Contributing

```bash
uv sync --locked --all-groups
uv run ruff format --check modal_agents tests agents hooks
uv run ruff check modal_agents tests agents hooks
uv run mypy
uv run basedpyright
uv run coverage run -m pytest
uv run coverage report
uv build
```

Public CI checks formatting, lint, at least 80% branch-aware coverage, wheel
installation, and tests on Python 3.12 and 3.14. It skips the three SDK-dependent
test modules; run the full suite and both type checkers locally with preview access. Tests cover signatures, guided setup, readiness, smoke failure/cleanup paths, and mocked
cloud services. Run the [live smoke checklist](USAGE.md#live-smoke-check) to verify
deployment and executor connectivity before production or after executor upgrades.
