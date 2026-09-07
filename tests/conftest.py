"""Shared pytest fixtures.

The unit suite is deliberately hermetic: no network access and no dependency
on built fonts or downloaded upstream archives. Anything a test needs is
either checked in under ``sources/``/``qa/`` or synthesised in the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository checkout."""
    return REPO_ROOT
