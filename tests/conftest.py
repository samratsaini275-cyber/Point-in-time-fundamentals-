"""Shared fixtures. One store build per test session (uses cached SEC JSON)."""
import pytest

from pit.store import build_store


@pytest.fixture(scope="session")
def con():
    return build_store()
