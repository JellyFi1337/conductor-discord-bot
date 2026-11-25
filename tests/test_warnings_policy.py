from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from conductor.domain import discord_limits

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_turns_conductor_deprecations_into_errors() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "error::DeprecationWarning:conductor" in text
    assert "error::PendingDeprecationWarning:conductor" in text


def test_deprecation_from_conductor_module_is_error() -> None:
    with pytest.raises(DeprecationWarning):
        warnings.warn_explicit(
            "x",
            DeprecationWarning,
            filename=discord_limits.__file__,
            lineno=1,
            module=discord_limits.__name__,
        )
