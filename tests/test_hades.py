"""
Quality gate — Hades compliance filter.

These tests protect real money. A failure here means ZEUS could
trade a sanctioned entity or ESG-blocked company. All must pass.
"""

from datetime import datetime, timezone

import pytest

from agents.hades import HadesAgent
from core.types import RawSignal, Severity, SignalCategory


def _sig(text: str, tickers: list[str] = None, signal_id: str = "test") -> RawSignal:
    return RawSignal(
        signal_id=signal_id,
        source_url="https://mock",
        headline=text,
        summary=text,
        published_at=datetime.now(timezone.utc),
        category=SignalCategory.POSITIVE_NEWS,
        severity=Severity.MEDIUM,
        affected_tickers=tickers or ["AAPL"],
        raw_text=text,
        supplier="TestCorp",
    )


@pytest.fixture
def hades():
    return HadesAgent()


# ── OFAC kills ─────────────────────────────────────────────────────────────────

class TestOfacKills:
    def test_rusal_killed(self, hades):
        assert hades.filter(_sig("RUSAL announces production increase")) is None

    def test_sberbank_killed(self, hades):
        assert hades.filter(_sig("Sberbank expands into Europe")) is None

    def test_rosneft_killed(self, hades):
        assert hades.filter(_sig("ROSNEFT signs new pipeline contract")) is None

    def test_ofac_case_insensitive(self, hades):
        assert hades.filter(_sig("rusal aluminium output up 10%")) is None

    def test_ofac_in_summary_kills(self, hades):
        sig = _sig("Supply chain update")
        sig.raw_text = "RUSAL is the primary supplier in this chain"
        assert hades.filter(sig) is None

    def test_clean_supplier_passes(self, hades):
        assert hades.filter(_sig("NVIDIA announces new GPU architecture")) is not None

    def test_unrelated_ru_word_does_not_kill(self, hades):
        """'Russia' alone should not kill — only exact OFAC entity names."""
        result = hades.filter(_sig("Russia passes new semiconductor regulation"))
        assert result is not None


# ── ESG downgrades ─────────────────────────────────────────────────────────────

class TestEsgDowngrades:
    def test_tobacco_downgraded_not_killed(self, hades):
        result = hades.filter(_sig("Philip Morris tobacco division reports earnings"))
        assert result is not None
        assert result.esg_flag is True
        assert result.compliance_score == pytest.approx(0.4)
        assert result.downgraded is True

    def test_coal_downgraded(self, hades):
        result = hades.filter(_sig("Glencore coal mining expansion planned"))
        assert result is not None
        assert result.esg_flag is True

    def test_weapons_downgraded(self, hades):
        result = hades.filter(_sig("Raytheon weapons contract awarded"))
        assert result is not None
        assert result.esg_flag is True

    def test_cluster_munition_downgraded(self, hades):
        result = hades.filter(_sig("Textron cluster munition production halted"))
        assert result is not None
        assert result.esg_flag is True

    def test_clean_signal_score_is_1(self, hades):
        result = hades.filter(_sig("SAP releases new enterprise software"))
        assert result is not None
        assert result.compliance_score == pytest.approx(1.0)
        assert result.esg_flag is False
        assert result.downgraded is False


# ── Blocked tickers ────────────────────────────────────────────────────────────

class TestBlockedTickers:
    def test_blocked_ticker_kills(self):
        hades = HadesAgent(blocked_tickers={"XBAD"})
        assert hades.filter(_sig("Some news", tickers=["XBAD"])) is None

    def test_non_blocked_ticker_passes(self):
        hades = HadesAgent(blocked_tickers={"XBAD"})
        assert hades.filter(_sig("Some news", tickers=["AAPL"])) is not None

    def test_mixed_tickers_kills_if_any_blocked(self):
        hades = HadesAgent(blocked_tickers={"XBAD"})
        assert hades.filter(_sig("News", tickers=["AAPL", "XBAD"])) is None


# ── Audit trail ────────────────────────────────────────────────────────────────

class TestAuditTrail:
    def test_notes_populated_on_ofac_kill(self, hades):
        sig = _sig("RUSAL reports record output")
        result = hades.filter(sig)
        assert result is None  # killed — notes only visible in logs

    def test_notes_populated_on_esg_flag(self, hades):
        result = hades.filter(_sig("Big tobacco company revenue up"))
        assert result is not None
        assert any("ESG" in n or "tobacco" in n.lower() for n in result.notes)

    def test_clean_pass_has_no_notes(self, hades):
        result = hades.filter(_sig("NVIDIA GPU supply chain update"))
        assert result is not None
        assert result.notes == []

    def test_filtered_signal_preserves_original(self, hades):
        sig = _sig("SAP cloud revenue up 20%")
        result = hades.filter(sig)
        assert result is not None
        assert result.headline == sig.headline
        assert result.supplier == sig.supplier
        assert result.category == sig.category


# ── FilteredSignal pass-throughs ───────────────────────────────────────────────

class TestFilteredSignalPassthroughs:
    def test_affected_tickers_preserved(self, hades):
        result = hades.filter(_sig("news", tickers=["MSFT", "GOOGL"]))
        assert result.affected_tickers == ["MSFT", "GOOGL"]

    def test_category_preserved(self, hades):
        sig = _sig("news")
        sig.category = SignalCategory.SUPPLIER_DISRUPTION
        result = hades.filter(sig)
        assert result.category == SignalCategory.SUPPLIER_DISRUPTION

    def test_severity_preserved(self, hades):
        sig = _sig("critical news")
        sig.severity = Severity.CRITICAL
        result = hades.filter(sig)
        assert result.severity == Severity.CRITICAL
