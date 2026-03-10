# Contributing to Oracle3

Thanks for your interest in contributing! Here is everything you need to get started.

## Setup

```bash
git clone https://github.com/YichengYang-Ethan/oracle3.git
cd oracle3
poetry install --with dev,test
```

## Running Tests

```bash
pytest tests/ -v
pytest tests/ --cov=oracle3 --cov-report=html
```

## Code Style

- Python 3.10+ with type annotations everywhere
- Use single quotes for strings
- Format and lint before committing:

```bash
ruff check . && ruff format .
```

## Pull Request Process

1. Create a feature branch from `main`
2. Write tests for any new functionality
3. Make sure all tests pass (`pytest tests/ -v`)
4. Run `ruff check . && ruff format .` with no errors
5. Open a PR with a clear description of the change
6. One approval required before merge

## Project Layout

See the `Project Structure` section in `README.md` for an overview of the
codebase. Strategy contributions go in `oracle3/strategy/contrib/`.

## Questions?

Open an issue or start a discussion on the repo.
