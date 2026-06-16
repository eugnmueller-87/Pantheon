"""
Anthropic model IDs are centralised in ZeusConfig (not hardcoded in 4 call
sites), overridable via settings.json, and verified at boot with a dry-run.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.zeus import ZeusConfig, ZeusOrchestrator


def test_no_hardcoded_model_string_in_zeus():
    """Acceptance #4: no literal 'claude-sonnet' model string in call sites.

    The only allowed occurrences are the config-field defaults and the
    settings-override guards (assignments), not inline messages.create(model=...).
    """
    src = Path("agents/zeus.py").read_text(encoding="utf-8")
    # Every model= passed to a create() call must reference self.config, never a literal.
    bad = re.findall(r'model\s*=\s*["\']claude-[\w.-]+["\']', src)
    assert bad == [], f"hardcoded model literal in a call: {bad}"


def _build_zeus(config, settings):
    with patch("agents.zeus.KnowledgeBase"), \
         patch("agents.zeus.CircuitBreaker"), \
         patch("agents.zeus.Watchdog"), \
         patch("agents.zeus.MilestoneManager"), \
         patch("agents.zeus.ApolloAgent"), \
         patch("agents.zeus.IcarusAgent"), \
         patch("agents.zeus.HadesAgent"), \
         patch("agents.zeus.ArtemisAgent"), \
         patch("agents.zeus.PythiaAgent"), \
         patch("agents.zeus.AresMockAgent"), \
         patch("agents.zeus.ArgusAgent"), \
         patch("agents.zeus.RedisBridge"), \
         patch("agents.zeus.SeniorityEvaluator"), \
         patch("agents.zeus.Watchdog.start"), \
         patch("agents.zeus.ZeusOrchestrator._run_seniority_evaluation"), \
         patch("agents.zeus.anthropic.Anthropic") as mock_anthropic, \
         patch("config.settings.load_settings", return_value=settings):
        zeus = ZeusOrchestrator(config)
    return zeus, mock_anthropic


def _base_settings(**over):
    s = {"account_equity": 4000.0, "mock_execution": True, "use_llm_reasoning": True}
    s.update(over)
    return s


def test_config_picks_up_model_override(monkeypatch):
    monkeypatch.setenv("ZEUS_SKIP_MODEL_CHECK", "1")  # skip the live dry-run
    cfg = ZeusConfig(default_account_equity=4000.0, mock_execution=True)
    zeus, _ = _build_zeus(cfg, _base_settings(
        anthropic_model_director="claude-opus-4-8",
        anthropic_model_debate="claude-haiku-4-5",
    ))
    assert zeus.config.anthropic_model_director == "claude-opus-4-8"
    assert zeus.config.anthropic_model_debate == "claude-haiku-4-5"


def test_default_model_is_current_sonnet(monkeypatch):
    monkeypatch.setenv("ZEUS_SKIP_MODEL_CHECK", "1")
    cfg = ZeusConfig(default_account_equity=4000.0, mock_execution=True)
    zeus, _ = _build_zeus(cfg, _base_settings())
    assert zeus.config.anthropic_model_director == "claude-sonnet-4-6"


def test_dry_run_failure_refuses_to_start(monkeypatch):
    """A bad model ID must raise at construction, not silently fall back."""
    monkeypatch.delenv("ZEUS_SKIP_MODEL_CHECK", raising=False)
    cfg = ZeusConfig(default_account_equity=4000.0, mock_execution=True,
                     use_llm_reasoning=True)
    # Make the dry-run messages.create raise.
    with patch("agents.zeus.KnowledgeBase"), \
         patch("agents.zeus.CircuitBreaker"), patch("agents.zeus.Watchdog"), \
         patch("agents.zeus.MilestoneManager"), patch("agents.zeus.ApolloAgent"), \
         patch("agents.zeus.IcarusAgent"), patch("agents.zeus.HadesAgent"), \
         patch("agents.zeus.ArtemisAgent"), patch("agents.zeus.PythiaAgent"), \
         patch("agents.zeus.AresMockAgent"), patch("agents.zeus.ArgusAgent"), \
         patch("agents.zeus.RedisBridge"), patch("agents.zeus.SeniorityEvaluator"), \
         patch("agents.zeus.Watchdog.start"), \
         patch("agents.zeus.ZeusOrchestrator._run_seniority_evaluation"), \
         patch("config.settings.load_settings", return_value=_base_settings()), \
         patch("agents.zeus.anthropic.Anthropic") as mock_anthropic:
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("not_found_error: bad model")
        mock_anthropic.return_value = client
        with pytest.raises(RuntimeError, match="unusable"):
            ZeusOrchestrator(cfg)
