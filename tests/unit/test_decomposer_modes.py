"""Decomposer mode flag (RoC-3a). Real env via monkeypatch, no mocks."""

from __future__ import annotations

import pytest

from apecx_integration.composition.decomposition.modes import resolve_decomposer_mode


def test_default_is_plan_returner(monkeypatch):
    monkeypatch.delenv("APECX_EO_DECOMPOSER_MODE", raising=False)
    assert resolve_decomposer_mode() == "plan_returner"


def test_env_honored(monkeypatch):
    monkeypatch.setenv("APECX_EO_DECOMPOSER_MODE", "auto_solver")
    assert resolve_decomposer_mode() == "auto_solver"
    monkeypatch.setenv("APECX_EO_DECOMPOSER_MODE", "PLAN_RETURNER")  # case-insensitive
    assert resolve_decomposer_mode() == "plan_returner"


def test_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("APECX_EO_DECOMPOSER_MODE", "auto_solver")
    assert resolve_decomposer_mode("plan_returner") == "plan_returner"


def test_invalid_raises_loudly(monkeypatch):
    monkeypatch.setenv("APECX_EO_DECOMPOSER_MODE", "garbage")
    with pytest.raises(ValueError, match="APECX_EO_DECOMPOSER_MODE"):
        resolve_decomposer_mode()


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APECX_EO_DECOMPOSER_MODE", "   ")
    assert resolve_decomposer_mode() == "plan_returner"
