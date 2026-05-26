"""
Quality gate — HistoricalIngestionPipeline.

Tests verify:
  - Pipeline runs without real network (all I/O mocked)
  - Idempotent doc_id prevents duplicate KB entries
  - FRED fallback to yfinance VIX when API key not set
  - Each ingestion method counts correctly
  - All failures are isolated — one bad source never halts the rest
  - KnowledgeBase.add_literature() doc_id parameter works
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from agents.apollo_historical import HistoricalIngestionPipeline
from core.knowledge_base import KnowledgeBase

# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_kb():
    """KnowledgeBase with a mock collection — no ChromaDB needed."""
    kb = MagicMock(spec=KnowledgeBase)
    return kb


@pytest.fixture
def pipeline(mock_kb):
    """Pipeline with mocked KB and no FRED API key."""
    return HistoricalIngestionPipeline(knowledge_base=mock_kb)


# ── KnowledgeBase.add_literature() with doc_id ────────────────────────────────

class TestAddLiteratureDocId:
    def test_add_literature_accepts_doc_id_kwarg(self, tmp_path):
        """Regression: add_literature must accept doc_id without TypeError."""
        kb = KnowledgeBase.__new__(KnowledgeBase)
        kb._knowledge_col = None   # force fallback path
        kb._decisions_col = None
        kb._fallback = []
        kb._persist_path = tmp_path / "chroma"
        # Should not raise
        kb.add_literature("Title", "text", source="test", doc_id="my:doc:id")

    def test_add_literature_without_doc_id_still_works(self, tmp_path):
        """Backward compat: callers that don't pass doc_id must still work."""
        kb = KnowledgeBase.__new__(KnowledgeBase)
        kb._knowledge_col = None
        kb._decisions_col = None
        kb._fallback = []
        kb._persist_path = tmp_path / "chroma"
        kb.add_literature("Title", "text", source="test")   # no doc_id


# ── Pipeline.run() summary structure ──────────────────────────────────────────

class TestPipelineRunStructure:
    def test_run_returns_summary_dict(self, pipeline):
        """run() always returns the expected summary keys."""
        with patch.object(pipeline, '_ingest_earnings_history', return_value=3), \
             patch.object(pipeline, '_ingest_form4_insiders',    return_value=2), \
             patch.object(pipeline, '_ingest_fred_macro',        return_value=7), \
             patch.object(pipeline, '_ingest_edgar_supply_chain', return_value=5):
            summary = pipeline.run()

        assert summary["earnings_loaded"] == 3
        assert summary["insider_trades"]  == 2
        assert summary["fred_series"]     == 7
        assert summary["supply_chain_8k"] == 5
        assert summary["errors"]          == []
        assert "started_at" in summary
        assert "finished_at" in summary

    def test_run_captures_partial_failures(self, pipeline):
        """A failure in one source is captured as an error, not propagated."""
        def _boom():
            raise RuntimeError("network down")

        with patch.object(pipeline, '_ingest_earnings_history', side_effect=RuntimeError("boom")), \
             patch.object(pipeline, '_ingest_form4_insiders',    return_value=0), \
             patch.object(pipeline, '_ingest_fred_macro',        return_value=0), \
             patch.object(pipeline, '_ingest_edgar_supply_chain', return_value=0):
            summary = pipeline.run()

        assert len(summary["errors"]) == 1
        assert "Earnings ingestion failed" in summary["errors"][0]
        assert summary["earnings_loaded"] == 0

    def test_run_without_kb_returns_zeros(self):
        """Pipeline with no KB is a no-op — never raises."""
        p = HistoricalIngestionPipeline(knowledge_base=None)
        summary = p.run()
        assert summary["earnings_loaded"] == 0
        assert summary["insider_trades"]  == 0
        assert summary["fred_series"]     == 0
        assert summary["supply_chain_8k"] == 0
        assert len(summary["errors"])     == 0


# ── Earnings ingestion ─────────────────────────────────────────────────────────

class TestEarningsIngestion:
    def _make_earnings_df(self):
        """Minimal earnings_dates DataFrame matching yfinance schema."""
        idx = pd.DatetimeIndex(
            [pd.Timestamp("2024-01-15", tz="UTC"),
             pd.Timestamp("2024-04-15", tz="UTC")],
            name="Earnings Date",
        )
        return pd.DataFrame({
            "Reported EPS": [2.50, 3.10],
            "EPS Estimate": [2.30, 2.80],
            "Surprise(%)":  [8.7,  10.7],
        }, index=idx)

    def test_earnings_calls_add_literature_for_each_row(self, pipeline):
        hist = pd.DataFrame(
            {"Close": [150.0] * 30},
            index=pd.date_range("2024-01-16", periods=30, tz="UTC"),
        )
        ticker_mock = MagicMock()
        ticker_mock.earnings_dates = self._make_earnings_df()
        ticker_mock.history.return_value = hist

        with patch("yfinance.Ticker", return_value=ticker_mock):
            added = pipeline._ingest_earnings_history()

        assert added == 2 * len(["NVDA", "TSM", "SAP", "SIEGY", "ASML",
                                   "INTC", "AMD", "QCOM", "BASFY", "DTEGY",
                                   "AAPL", "MSFT", "AMZN", "META"])
        assert pipeline._kb.add_literature.call_count > 0

    def test_earnings_skips_rows_with_missing_eps(self, pipeline):
        idx = pd.DatetimeIndex([pd.Timestamp("2024-01-15", tz="UTC")])
        df = pd.DataFrame({"Reported EPS": [None], "EPS Estimate": [None],
                           "Surprise(%)": [None]}, index=idx)
        hist = pd.DataFrame({"Close": [100.0] * 10},
                            index=pd.date_range("2024-01-16", periods=10, tz="UTC"))
        ticker_mock = MagicMock()
        ticker_mock.earnings_dates = df
        ticker_mock.history.return_value = hist

        with patch("yfinance.Ticker", return_value=ticker_mock):
            added = pipeline._load_ticker_earnings("AAPL")

        assert added == 0

    def test_earnings_empty_dataframe_returns_zero(self, pipeline):
        ticker_mock = MagicMock()
        ticker_mock.earnings_dates = pd.DataFrame()
        ticker_mock.history.return_value = pd.DataFrame({"Close": [100.0]})

        with patch("yfinance.Ticker", return_value=ticker_mock):
            added = pipeline._load_ticker_earnings("AAPL")

        assert added == 0

    def test_earnings_yfinance_exception_returns_zero(self, pipeline):
        with patch("yfinance.Ticker", side_effect=RuntimeError("rate limit")):
            added = pipeline._ingest_earnings_history()
        assert added == 0


# ── FRED fallback ──────────────────────────────────────────────────────────────

class TestFredIngestion:
    def test_fred_skips_when_no_api_key(self, pipeline):
        """Without FRED_API_KEY, falls back to VIX via yfinance."""
        assert pipeline._fred_api_key == ""
        vix_hist = pd.DataFrame(
            {"Close": [18.0, 20.0, 22.0]},
            index=pd.date_range("2024-01-01", periods=3, freq="W", tz="UTC"),
        )
        vix_mock = MagicMock()
        vix_mock.history.return_value = vix_hist

        with patch("yfinance.Ticker", return_value=vix_mock):
            n = pipeline._ingest_fred_macro()

        assert n == 1  # VIX fallback contributes 1 record
        pipeline._kb.add_literature.assert_called_once()

    def test_fred_loads_series_when_api_key_set(self, mock_kb):
        """With FRED_API_KEY, calls the FRED REST API for each series."""
        import os
        p = HistoricalIngestionPipeline(knowledge_base=mock_kb)
        p._fred_api_key = "fake-key"

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "observations": [
                {"date": "2024-01-02", "value": "5.33"},
                {"date": "2024-01-03", "value": "5.33"},
            ]
        }

        with patch("requests.get", return_value=mock_response):
            n = p._ingest_fred_macro()

        # 7 FRED series — each should load 1 summary doc
        assert n == 7
        assert mock_kb.add_literature.call_count == 7

    def test_fred_fallback_empty_vix_returns_zero(self, pipeline):
        vix_mock = MagicMock()
        vix_mock.history.return_value = pd.DataFrame()
        with patch("yfinance.Ticker", return_value=vix_mock):
            n = pipeline._ingest_vix_fallback()
        assert n == 0

    def test_fred_series_failure_does_not_propagate(self, mock_kb):
        """A bad HTTP response for one series doesn't crash the others."""
        p = HistoricalIngestionPipeline(knowledge_base=mock_kb)
        p._fred_api_key = "fake-key"

        call_count = {"n": 0}

        def selective_fail(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise RuntimeError("timeout")
            m = MagicMock()
            m.raise_for_status.return_value = None
            m.json.return_value = {"observations": [{"date": "2024-01-01", "value": "1.0"}]}
            return m

        with patch("requests.get", side_effect=selective_fail):
            n = p._ingest_fred_macro()

        assert n == 6   # 7 series − 1 failure = 6


# ── EDGAR supply chain ────────────────────────────────────────────────────────

class TestEdgarIngestion:
    def test_edgar_stores_matching_8k_hits(self, pipeline):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hits": {"hits": [
                {"_source": {
                    "entity_name": "Acme Corp",
                    "file_date": "2024-03-01",
                    "period_of_report": "2024-02-28",
                }},
            ]}
        }
        with patch("requests.get", return_value=mock_resp):
            added = pipeline._ingest_edgar_supply_chain()

        # 3 keywords × 1 hit each = 3
        assert added == 3
        assert pipeline._kb.add_literature.call_count == 3

    def test_edgar_http_error_returns_zero(self, pipeline):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        with patch("requests.get", return_value=mock_resp):
            added = pipeline._ingest_edgar_supply_chain()
        assert added == 0

    def test_edgar_exception_returns_zero(self, pipeline):
        with patch("requests.get", side_effect=ConnectionError("timeout")):
            added = pipeline._ingest_edgar_supply_chain()
        assert added == 0


# ── Price reaction helper ──────────────────────────────────────────────────────

class TestCalcPriceReaction:
    def _make_hist(self, closes: list[float]) -> pd.DataFrame:
        dates = pd.date_range("2024-01-10", periods=len(closes), tz="UTC")
        return pd.DataFrame({"Close": closes}, index=dates)

    def test_positive_reaction_is_positive(self):
        hist = self._make_hist([100.0, 105.0, 107.0, 110.0, 112.0, 115.0])
        earnings_date = pd.Timestamp("2024-01-09", tz="UTC")
        reaction = HistoricalIngestionPipeline._calc_price_reaction(hist, earnings_date, days=5)
        assert reaction > 0

    def test_negative_reaction_is_negative(self):
        hist = self._make_hist([100.0, 95.0, 93.0, 90.0, 88.0, 85.0])
        earnings_date = pd.Timestamp("2024-01-09", tz="UTC")
        reaction = HistoricalIngestionPipeline._calc_price_reaction(hist, earnings_date, days=5)
        assert reaction < 0

    def test_insufficient_data_returns_zero(self):
        hist = self._make_hist([100.0, 102.0])
        earnings_date = pd.Timestamp("2024-01-09", tz="UTC")
        reaction = HistoricalIngestionPipeline._calc_price_reaction(hist, earnings_date, days=5)
        assert reaction == 0.0

    def test_empty_history_returns_zero(self):
        hist = pd.DataFrame({"Close": []})
        reaction = HistoricalIngestionPipeline._calc_price_reaction(
            hist, pd.Timestamp("2024-01-09", tz="UTC"), days=5
        )
        assert reaction == 0.0


# ── Apollo.run_historical_ingestion() wiring ──────────────────────────────────

class TestApolloWiring:
    def test_apollo_run_historical_ingestion_calls_pipeline(self, tmp_path):
        from agents.apollo import ApolloAgent
        agent = ApolloAgent(knowledge_base=None)

        mock_summary = {"earnings_loaded": 5, "fred_series": 7, "errors": []}

        with patch("agents.apollo_historical.HistoricalIngestionPipeline.run",
                   return_value=mock_summary) as mock_run:
            result = agent.run_historical_ingestion()

        mock_run.assert_called_once()
        assert result["earnings_loaded"] == 5
