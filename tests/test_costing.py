"""Unit test engine perhitungan — angka mengikuti tabel PRD §9."""
from decimal import Decimal as D

import pytest

from app.services import costing as c


@pytest.mark.parametrize("loaded,expected", [
    (10_000_000, D("555555.56")),   # UI/UX
    (12_500_000, D("694444.44")),   # Frontend
    (15_000_000, D("833333.33")),   # Backend / DevOps
])
def test_cost_rate_prd_table(loaded, expected):
    pmd = c.productive_md(20, D("0.9"))
    assert pmd == D("18.00")
    assert c.cost_rate_per_md(loaded, pmd) == expected


def test_loaded_monthly_cost():
    assert c.loaded_monthly_cost(12_000_000, 1_500_000, 750_000, 500_000, 250_000) == D("15000000.00")


def test_actual_md_and_labor_cost():
    md = c.actual_md(D("6"), D("8"))
    assert md == D("0.7500")
    assert c.labor_cost(md, D("833333.33")) == D("625000.00")


def test_productive_md_zero_rejected():
    with pytest.raises(ValueError):
        c.cost_rate_per_md(1, 0)
