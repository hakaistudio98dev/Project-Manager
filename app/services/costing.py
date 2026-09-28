"""Resource & Man-Day Engine (PRD §9) dan Actual Cost (PRD §13).

Semua perhitungan uang memakai Decimal, dibulatkan ROUND_HALF_UP ke 2 desimal.
Fungsi-fungsi di sini murni (tanpa I/O) supaya mudah di-unit-test.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ResourceRate

CENT = Decimal("0.01")
MD_Q = Decimal("0.0001")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def loaded_monthly_cost(salary, benefits=0, equipment=0, software=0, other=0) -> Decimal:
    """Loaded Monthly Cost = Salary + Benefits + Equipment + Software + Other Employee Cost"""
    return money(sum(Decimal(str(v or 0)) for v in (salary, benefits, equipment, software, other)))


def productive_md(working_days, productive_capacity) -> Decimal:
    """Productive MD = Working Days × Productive Capacity"""
    return (Decimal(str(working_days)) * Decimal(str(productive_capacity))).quantize(CENT, ROUND_HALF_UP)


def cost_rate_per_md(loaded_cost, prod_md) -> Decimal:
    """Cost Rate / MD = Loaded Monthly Cost / Productive MD"""
    prod_md = Decimal(str(prod_md))
    if prod_md <= 0:
        raise ValueError("Productive MD harus > 0")
    return money(Decimal(str(loaded_cost)) / prod_md)


def labor_cost(md, rate) -> Decimal:
    """Project Labor Cost = MD × Cost Rate"""
    return money(Decimal(str(md)) * Decimal(str(rate)))


def actual_md(hours, standard_hours_per_day) -> Decimal:
    """Actual MD = Hours / Standard Hours Per Day"""
    return (Decimal(str(hours)) / Decimal(str(standard_hours_per_day))).quantize(MD_Q, ROUND_HALF_UP)


def rate_effective_on(db: Session, employee_id: int, on: date) -> ResourceRate | None:
    """Rate versi terbaru dengan effective_date <= tanggal."""
    return db.scalar(select(ResourceRate)
                     .where(ResourceRate.employee_id == employee_id, ResourceRate.effective_date <= on)
                     .order_by(ResourceRate.effective_date.desc()).limit(1))
