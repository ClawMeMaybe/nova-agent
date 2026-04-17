# Contributing to Nova Agent

Thank you for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/nova-agent/nova-agent.git
cd nova-agent

# Install with dev dependencies
pip install -e ".[dev]"

# Set API key
export ANTHROPIC_API_KEY=your-key-here

# Run tests
pytest tests/
```

## How to Contribute

1. **Fork** the repository on GitHub
2. **Create a branch** for your feature or fix: `git checkout -b my-feature`
3. **Make your changes** and add tests if applicable
4. **Run tests**: `pytest tests/` — all must pass
5. **Submit a pull request** with a clear description of what you changed and why

## Reporting Issues

- **Bug reports**: Open a GitHub Issue with reproduction steps, expected behavior, and actual behavior
- **Feature requests**: Open a GitHub Issue with the "feature" label and describe the use case

## Code Style

- Python 3.10+ (use type hints where practical)
- Docstrings on public functions (short, one-line max)
- No emojis in code
- Follow existing patterns in the codebase

## Commit Messages

Use conventional commit style:
- `feat: add new feature`
- `fix: resolve bug`
- `refactor: improve code structure`
- `docs: update documentation`
- `test: add test coverage`

## Areas That Need Contributions

- **Test coverage**: The memory engine, cron system, and autonomous mode need more tests
- **Documentation**: Examples, tutorials, architecture deep-dives
- **New gateways**: Slack, Matrix, IRC, or other messaging platforms
- **Performance**: SQLite query optimization, context prompt compression
- **Security**: SQL injection testing, credential management improvements