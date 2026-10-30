"""Shared pytest setup for the bridge.

Makes ``bridge/`` importable when pytest is invoked from either level, and clears the
process-wide login rate limiter between tests (it is keyed per client address, so one
test deliberately exhausting it would lock out the next).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elegoo_bridge import auth as auth_module  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    auth_module._attempts.clear()  # noqa: SLF001 - test isolation for module state
    yield
    auth_module._attempts.clear()  # noqa: SLF001
