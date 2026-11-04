"""Shared pytest fixtures for the Obsidian Nexus test suite."""

from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
