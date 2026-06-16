"""
Pytest wrapper for the replay harness.

Runs every fixture row through the real pipeline and asserts the binding
constraint matches `expected_stage`. Add new fixtures by appending rows to
`fixtures.csv` — no new test code required.

Run:  pytest tests/replay/test_replay.py -v
"""
from pathlib import Path

import pytest

from .harness import ReplayHarness, load_csv

FIXTURES = Path(__file__).parent / "fixtures.csv"


def _ids(rows):
    return [f"{r.signal_id}:{r.expected_stage or 'executed'}" for r in rows]


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return ReplayHarness(tmp_path_factory.mktemp("replay"))


@pytest.mark.parametrize("row", load_csv(FIXTURES), ids=_ids(load_csv(FIXTURES)))
def test_signal_killed_at_expected_stage(harness, row):
    result = harness.run_one(row)
    assert result.actual_stage == row.expected_stage, (
        f"\n  signal:   {row.headline}"
        f"\n  expected: {row.expected_stage or '(executed)'}"
        f"\n  actual:   {result.actual_stage or '(executed)'}"
        f"\n  detail:   {result.detail}"
        f"\n  note:     {row.note}"
    )
