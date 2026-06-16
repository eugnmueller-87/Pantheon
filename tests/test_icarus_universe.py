"""
Icarus drops private / non-tradeable names cleanly (out_of_universe) rather than
mis-mapping them onto a loosely-adjacent public ticker. SpaceX is the canonical
case: it is private (SPCX is an SPV, not SpaceX equity), so a SpaceX headline
must NOT become a TSLA signal.
"""

from __future__ import annotations

from agents.icarus import _is_private_supplier, _resolve_ticker


def test_spacex_is_private_not_tsla():
    assert _is_private_supplier("SpaceX") is True
    assert _is_private_supplier("Space Exploration Technologies Corp") is True
    # Must NOT resolve to a public ticker (the bug we're preventing).
    assert _resolve_ticker("SpaceX") != "TSLA"


def test_word_boundary_and_case_insensitive():
    assert _is_private_supplier("SPACEX eyes funding") is True   # word + case
    assert _is_private_supplier("starlink expansion") is True
    assert _is_private_supplier("OpenAI") is True


def test_public_companies_not_flagged():
    for name in ("NVIDIA", "Tesla", "Apple", "Boeing", "Alphabet", "Meta"):
        assert _is_private_supplier(name) is False, name


def test_lookalikes_not_false_dropped():
    # Word-boundary matching must NOT catch real public/other names that merely
    # contain a private brand as a substring.
    for name in ("Revolution Medicines", "Chimera Investment", "Canvas Inc",
                 "Stripe Solutions Ltd", "Discordant Capital"):
        assert _is_private_supplier(name) is False, name


def test_empty_supplier_not_flagged():
    assert _is_private_supplier("") is False
    assert _is_private_supplier(None) is False


def test_distinctive_private_names_flagged():
    for name in ("ByteDance", "TikTok", "Databricks", "xAI", "Neuralink", "SHEIN"):
        assert _is_private_supplier(name) is True, name
