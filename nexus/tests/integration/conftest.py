"""pytest configuration for integration tests.

Integration tests require a live Claude Code session and are skipped by default.
Run with: pytest tests/integration/ --real-claude   (Phase 1 tests)
          pytest tests/integration/ --real-providers
          (Phase 2a tests — needs OpenRouter key + Ollama)
"""
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--real-claude",
        action="store_true",
        default=False,
        help="Run tests that require a live Claude Code + Pro subscription",
    )
    parser.addoption(
        "--real-providers",
        action="store_true",
        default=False,
        help="Run tests that require live Claude Code, OpenRouter API key, and local Ollama",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_claude: mark test as requiring a live Claude Code session (skipped by default)",
    )
    config.addinivalue_line(
        "markers",
        "real_providers: mark test as requiring all Phase 2a providers (skipped by default)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    run_claude = config.getoption("--real-claude")
    run_providers = config.getoption("--real-providers")

    skip_claude = pytest.mark.skip(reason="pass --real-claude to run integration tests")
    skip_providers = pytest.mark.skip(
        reason="pass --real-providers to run Phase 2a integration tests"
    )

    for item in items:
        if item.get_closest_marker("real_claude") and not run_claude:
            item.add_marker(skip_claude)
        if item.get_closest_marker("real_providers") and not run_providers:
            item.add_marker(skip_providers)
